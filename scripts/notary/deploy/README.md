# Шлюз Ватсона: что осталось сделать НА VPS (долг на деплой владельца)

Код шлюза (`../gateway.py`, `../lib/orders.py`) и его пробы сданы фазой 9 и лежат в
репозитории. Всё, что ниже, физически требует живой машины: выкатывает **владелец**,
исполнитель фазы на VPS не ходил и ходить не имел права.

Контракт ручек для портала — [`../docs/gateway-contract.md`](../docs/gateway-contract.md).

---

## 1. Секрет (без него шлюз отвечает 503 всем — это норма, а не поломка)

```bash
# на VPS, от dev
umask 077
printf 'MEETING_NOTARY_GATEWAY_TOKEN=%s\n' "$(openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-48)" \
  > /srv/meeting-notary/.env.gateway
chmod 600 /srv/meeting-notary/.env.gateway
chown dev:dev /srv/meeting-notary/.env.gateway
```

* Минимум 32 знака — короче шлюз считает себя **не**настроенным и закрывается.
* Файл читаем только владельцем. Если положить секрет в `MEETING_NOTARY_GATEWAY_TOKEN_FILE`
  с правами 644, шлюз тоже закроется: секрет, который читает кто угодно, — не секрет.
* В репозиторий значение **не попадает никогда**. Тот же токен кладётся порталу
  в переменную окружения Amvera (не в код, не в `.env` репозитория).

## 2. Юнит

```bash
scp scripts/notary/deploy/meeting-notary-gateway.service dev@vps:/tmp/
sudo cp /tmp/meeting-notary-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-notary-gateway
systemctl status meeting-notary-gateway
```

⚠️ `make deploy-notary` возит **только** `vexa/scripts/notary/**`. Юниты и Caddy он
не возит (инцидент 2026-06-30) — шаги выше делаются руками.

Зависимостей сверх стандартной библиотеки у самого шлюза нет; `pyyaml` (для `/status`)
и headless-chromium (для `?format=pdf`) уже стоят в `/home/dev/meeting-notary/venv`.

### 2а. 🔴 Юнит ОБНОВЛЁН (правка приёмки Ф10d) — переставить обязательно

Ручка правок с портала (`POST /protocol/edit`) живёт в процессе шлюза и пишет то же
окно правок, что листенер. Юнит намеренно не грузит `.env.notary`, поэтому раньше
процесс уходил на **дефолты из кода** — а дефолт каталога правок это
`/opt/meeting-notary/_feedback_edits`, каталог root'а. Под `dev` запись падает, и
**каждая правка теряется без ack** — ровно инцидент 2026-06-08, только через новую
дверь. В юнит добавлены `MEETING_NOTARY_FEEDBACK_DIR`, `TELEMOST_PROTOCOL_DIR`,
`MEETING_NOTARY_DELIVERED_ROOTS`, `ENABLE_PORTAL_ORDERS`, `ENABLE_FEEDBACK_EDITS`,
`ENABLE_QUOTE_POLICY`, окна правок, `MEETING_NOTARY_LOG_DIR`,
`MEETING_NOTARY_MAX_CONCURRENT_BOTS` и `ReadWritePaths` на каталог правок.

Перед `daemon-reload`:

```bash
# каталоги должны существовать и принадлежать dev — иначе запись упадёт молча
sudo -u dev mkdir -p /home/dev/meeting-notary/_tmp/_feedback_edits /home/dev/meeting-notary/logs
sudo -u dev test -w /home/dev/meeting-notary/_tmp/_feedback_edits && echo "каталог правок писабелен"
```

⚠️ Значения путей и окон **продублированы** из `.env.notary` — правишь там, правь и в
юните. Сторож `test_gateway_unit_declares_env` сверяет список переменных юнита с
реальными `os.environ.get` в коде ручек и краснеет, если появилась необъявленная.

⚠️ `MEETING_NOTARY_MAX_CONCURRENT_BOTS=2` в юните шлюза обязан совпадать со значением
у runner'а: разъедется — портал будет обещать слот, которого нет.

## 3. Caddy + DNS

1. A-запись `watson.anzhee.ru` → IP VPS (авто-TLS требует, чтобы имя резолвилось
   и порт 80 был открыт).
2. Блок из `meeting-notary-gateway.caddy` — в `/etc/caddy/Caddyfile`.
3. `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.

## 4. Проверка боем (после выкатки)

```bash
# на VPS: локально, без TLS
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     'http://127.0.0.1:8789/health?deep=1'      # ожидаем 200

# снаружи: через Caddy
curl -s -H "Authorization: Bearer $TOKEN" 'https://watson.anzhee.ru/health?deep=1' | jq
curl -s -o /dev/null -w '%{http_code}\n' 'https://watson.anzhee.ru/health'   # ожидаем 401
```

`?deep=1` возвращает 503, если планировщик молчит дольше 90 минут
(`MEETING_NOTARY_GATEWAY_SCHEDULER_STALE_MINUTES`), реестр нарядов битый или
раскрутка записи в элемент очереди сломалась. **Это и есть смысл проверки**: плоский
health отвечал 200 при мёртвой службе и стоил проекту трёх суток тишины (17.07).

## 5. Сторож в портале

Завести третью проверку по образцу двух соседних шлюзов: cron `*/30`, запрос
`GET https://watson.anzhee.ru/health?deep=1` с Bearer, алерт в Telegram при коде
≠ 200 два раза подряд. Токен — в переменной окружения портала.

## 6. 🔴 Слот авторизации Claude на этой машине — не трогать

Все 14 юнитов Ватсона живут на годовом `CLAUDE_CODE_OAUTH_TOKEN` из
`/srv/meeting-notary/.env.notary`. Шлюз к Claude **не аутентифицируется вовсе** — он
транспорт, и его юнит этот файл даже не читает. `claude setup-token` и `/login` на
VPS под тем же аккаунтом запрещены, **включая отладку**: один аккаунт в двух домашних
папках выбивает сам себя за 1–3 дня, и службы умирают молча (инциденты 26.06, 17.07).

## 7. Проверка всех служб — ЧЕРЕЗ СУТКИ, а не сразу

Сразу она ничего не докажет: выбивание токена проявляется через 1–3 дня. На следующий
день после выкатки: `systemctl list-units 'meeting-notary-*'` + одна реальная встреча
записалась и протокол пришёл.

## 8. Лимиты памяти соседних юнитов

`10-memory-limits.conf` — шаблон drop-in. Ставить **после замера** пика каждого юнита
(`systemd-cgtop -m`), сначала `MemoryHigh`, через неделю `MemoryMax`. Потолок, поставленный
на глаз, сам начнёт убивать рабочую службу.

## 9. Прирост диска — посчитать ДО массового включения

Модель (запись 16 кГц моно 16 бит = **115 МБ/час** WAV, ретеншн метельщика 14 дней):

```
пик_на_диске ≈ встреч_в_день × средняя_длительность_ч × 115 МБ × 14
```

| встреч/день | 1 ч каждая | пик WAV |
|---|---|---|
| 2 (сегодня) | 2 ч | ~3,2 ГБ |
| 5 | 5 ч | ~8,1 ГБ |
| 10 | 10 ч | ~16,1 ГБ |

Свободно было 25 ГБ из 75 (после чистки 12.08; до неё 9,8 ГБ). То есть **порог тревоги —
около 10 встреч в день**: дальше 14-дневный хвост WAV съедает остаток, а «ноль байт»
роняет все службы, включая нотариуса. Решение при подходе к порогу: сократить ретеншн
WAV до 7 дней (транскрипты и протоколы не трогать) — это ровно вдвое меньший хвост и
самый дешёвый шаг. Точные цифры снимать на машине: `df -h /` и `du -sh /srv/meeting-notary/*`.

## 10. Карта сред — ✅ уже внесена

`~/Projects/meeting-notary/.deploy-surfaces` обновлён фазой 9 (файл лежит вне
git-репозитория бота, рядом положена копия `.deploy-surfaces.bak`):

* новая среда **`caddy-reverse-proxy`** — конфигурация Caddy, которая до этого не была
  покрыта ни одной картой, хотя это единственная дверь снаружи;
* в **`host-systemd-units`** добавлены пути `vexa/scripts/notary/deploy/*.service` и
  `*.conf` — rsync привезёт их как файлы, но в `/etc/systemd/system/` не поставит.

## 11. ⚠️ Зеркало реестра на мак — добавить исключения

`~/.local/bin/meeting-notary-mirror.sh` (launchd на маке) тянет `/srv/meeting-notary/protocols/`
в `~/Projects/me/встречи/`. На маке это **тот же каталог**, что и реестр, а список
`--exclude` в скрипте неполон: в нём есть `.queue.json`, `.state.json`,
`.scheduler-last-success`, но **нет** `.orders.json`, `.dedup-winners.json`, `.pause-until`
и `*.lock`.

Сегодня это безвредно: `--delete` в команде нет. Но комментарий в самом скрипте
утверждает, что он там есть — то есть расхождение кода и намерения. Если кто-то однажды
приведёт команду к комментарию, зеркало снесёт реестр нарядов молча. Одна строка
исключений страхует от чужой будущей правки. Файл вне обоих репозиториев — правит владелец.
