# Шлюз Ватсона: что осталось сделать НА VPS (долг на деплой владельца)

> В этом файле три инструкции: шлюз портала (ниже), самопроверка бота в Т-10 и **выкатка
> ISS-60 «устойчивость к Телемосту»** — последний раздел, по одному слову владельца.

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

1. Рабочее имя — `watson.46-224-163-202.sslip.io`: оно само резолвится в IP
   VPS. У `watson.anzhee.ru` A-записи нет; подставлять его в настройки портала
   нельзя (боевой сбой 14.10.2026). Порт 80 нужен для выдачи сертификата.
2. Блок из `meeting-notary-gateway.caddy` — в `/etc/caddy/Caddyfile`.
3. `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.

## 4. Проверка боем (после выкатки)

```bash
# на VPS: локально, без TLS
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     'http://127.0.0.1:8789/health?deep=1'      # ожидаем 200

# снаружи: через Caddy
curl -s -H "Authorization: Bearer $TOKEN" 'https://watson.46-224-163-202.sslip.io/health?deep=1' | jq
curl -s -o /dev/null -w '%{http_code}\n' 'https://watson.46-224-163-202.sslip.io/health'   # ожидаем 401
```

`?deep=1` возвращает 503, если планировщик молчит дольше 90 минут
(`MEETING_NOTARY_GATEWAY_SCHEDULER_STALE_MINUTES`), реестр нарядов битый или
раскрутка записи в элемент очереди сломалась. **Это и есть смысл проверки**: плоский
health отвечал 200 при мёртвой службе и стоил проекту трёх суток тишины (17.07).

## 5. Сторож в портале

Завести третью проверку по образцу двух соседних шлюзов: cron `*/30`, запрос
`GET https://watson.46-224-163-202.sslip.io/health?deep=1` с Bearer, алерт в Telegram при коде
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

---

# Самопроверка бота в Т-10 и «нюх» версии Телемоста (ISS-60, фаза 2)

**Что это.** Каждый день в 04:40 МСК в запасную переговорку Т-10 заходят
«Тест-диктор» и бот — тем же образом и тем же кодом входа, что на встречах. После
обновления Телемоста бот проверяется тоже только утром, пока в Т-10 никого нет
(решение владельца 26.09): смену версии закрывает ближайшая ежедневная самопроверка,
начатая после того, как «нюх» увидел новую версию; не закрыла (пропуск, сбой самой
проверки, версия сменилась уже после 04:40) → внеплановая в 07:00–08:00 МСК. Днём
«нюх» только запоминает, что проверка нужна. Перепроверка красного, которую не удалось
сделать сразу, — тоже только в этом окне: днём самопроверка в Т-10 не заходит (ручной запуск —
исключение). Диктор говорит фразу с кодовым словом, бот её
записывает, самопроверка ищет слово в расшифровке (локальный whisper). Красное
перепроверяется вторым прогоном через 3 минуты, и только потом — сообщение
владельцу. Пока самопроверка красная, запускатель за ~10 минут до каждой встречи
предупреждает владельца: «включи запись в Телемосте». Логика и все решения —
`../lib/canary.py`, диктор — `../canary-kit/speaker.{sh,js}`.

| Файл | Что делает |
|---|---|
| `meeting-notary-canary.service` + `.timer` | самопроверка, 01:40 UTC = 04:40 МСК |
| `meeting-notary-telemost-sniff.service` + `.timer` | раз в час версия Телемоста; сменилась — проверка утром (ежедневной в 04:40 или внеплановой в 07:00–08:00 МСК) |

Состояние — `/srv/meeting-notary/state/canary/` (`health.json` «бот здоров/сломан»,
`runs/<run_id>.json` по каждому прогону, `telemost-version.json`, `warned.json`,
`journal.jsonl`). Записи прогонов — `/home/dev/meeting-notary/_tmp/canary/<run_id>/`
(вне папки записей бота: сборщик их не видит). Посмотреть: `python -m notary.canary status`.

## Установка (при выкатке, фаза 7 — сейчас не ставим)

> 🔴 При выкатке ISS-60 ставить **не по этому блоку, а по разделу «Выкатка ISS-60» ниже** (шаги 5–7):
> самопроверка судит бота по статусу входа, который пишет только новый образ, — на старом образе
> она красная и начнёт предупреждать владельца перед каждой встречей. Таймеры — только после
> переключения образа. Этот блок — справка по командам.

```bash
# на VPS, от dev; скрипты уже привезены обычной выкаткой скриптов нотариуса
sudo -u dev mkdir -p /srv/meeting-notary/state/canary /home/dev/meeting-notary/_tmp/canary /home/dev/meeting-notary/logs
# комната самопроверки должна быть в реестре: rooms.yaml → rooms: [{name: t10, url: …}]
grep -n "name: t10" /srv/meeting-notary/registry/rooms.yaml
cd /srv/meeting-notary/vexa
sudo cp scripts/notary/deploy/meeting-notary-canary.{service,timer} \
        scripts/notary/deploy/meeting-notary-telemost-sniff.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
# проверка вручную до таймеров — ровно тем окружением, что у таймера (боевой образ):
sudo systemctl start meeting-notary-canary.service; journalctl -u meeting-notary-canary -n 30 --no-pager
sudo systemctl enable --now meeting-notary-canary.timer meeting-notary-telemost-sniff.timer
systemctl list-timers 'meeting-notary-*'

# кандидат правки на ТЕСТОВОМ образе — только вывод команды и runs/, боевое
# состояние (health.json) и сообщения владельцу он не трогает:
sudo -u dev bash -c 'set -a; . /srv/meeting-notary/.env.notary; set +a; cd /srv/meeting-notary/vexa; \
  PYTHONPATH=scripts MEETING_NOTARY_LOCAL_DOCKER=1 \
  /home/dev/meeting-notary/venv/bin/python -m notary.canary run --reason manual --image vexa-bot:resil-…'
```

🔴 Не передавай `.env.notary` аргументами sudo (`sudo -u dev env $(grep … | xargs) …`):
sudo пишет команду вместе с аргументами в журнал авторизации сервера — все токены
окажутся в `journalctl`/`auth.log`. Окружение подгружает сам шелл внутри `bash -c`
(в журнал уходит только текст команды) или systemd (`systemctl start …service`).

⚠️ Выкатка скриптов юниты не ставит — только привозит файлы (см. §2 выше).
⚠️ Службы работают на питоне `/home/dev/meeting-notary/venv` (есть `requests` для
whisper и `pyyaml`), а не на `venv-cli` запускателя.
⚠️ В юнитах `KillMode=mixed`: при остановке или истёкшем сроке SIGTERM получает
только питон самопроверки — он выводит бота и диктора из Т-10 и убирает контейнеры.
Предупреждение перед встречами отдельного юнита не требует: оно живёт в тике
запускателя и включается выкаткой скриптов. Там же — тихая строка раз в сутки, если
самопроверка больше 30 часов не доходила до итога (умерла или не сработал таймер);
сразу после выкатки прогони её вручную (см. выше), иначе строка сошлётся на старые
тестовые прогоны.

## Как выключить

```bash
sudo systemctl disable --now meeting-notary-canary.timer meeting-notary-telemost-sniff.timer
# предупреждения перед встречами и строка «самопроверка давно не шла» — строка в
# /srv/meeting-notary/.env.notary (выключаешь таймеры — выключай и её):
MEETING_NOTARY_CANARY_WARN=0
```

Контейнеры самопроверки — `vexa-canary-speaker-*` / `vexa-canary-bot-*` с меткой
`meeting-notary.role=canary`; запускатель, сборщик и сторож их не видят. Прерванный
прогон (SIGTERM/SIGHUP, Ctrl-C) убирает свои контейнеры сам; если питон убили
SIGKILL — следующий прогон убирает оставшиеся контейнеры с этой меткой.

---

# Выкатка ISS-60 (устойчивость к Телемосту) — по одному слову владельца

**Что выкатывается.** Всё из ветки `telemost-resilience` (план
`plans/2026-09-23-telemost-update-resilience.md`, фазы 1–6б): сторож входа (повтор, сообщение
туда же, куда протокол, «бот не пришёл» порталу), самопроверка в Т-10 и «нюх» версии Телемоста
(после обновления — проверка только утром), предупреждение перед встречами, пока бот сломан, новый
образ бота (экран Телемоста 3.0, участники с плиток, микрофон, выход кнопкой, запасной путь входа,
второе ухо, `exec node`), замок ручной финализации и новая обёртка восстановления (встреча «лично
мне» по серии, протокол которой получает не только владелец, — вне памяти серии и не уходит
получателям по «пришлите протокол»), удаление всего звука встречи после доставки (необратимо,
см. §0 п.5 и §3), инструкция Альфреда и методичка восстановления. Три
части: **скрипты** (`scripts/notary`), **образ бота** (`vexa-bot:notarius-telemost`), **таймеры
самопроверки** — плюс строка в `.env.notary` и два файла инструкций на маке.

**Порядок важен.** Самопроверка судит бота по статусу входа, который пишет только новый образ:
на старом образе она покраснеет и начнёт предупреждать владельца перед каждой встречей. Поэтому
таймеры самопроверки включаются **после** переключения образа. Скрипты со старым образом
безопасны: без статуса входа сторож никого не останавливает и реагирует только на выход бота
с ошибкой (код 1) — один повтор, затем сообщение (журнал решений плана, 23.09). Шаги 3–7 —
одним заходом, примерно за час.

Все команды — с мака из `~/Projects/meeting-notary`. Выкатываем из рабочего дерева
`vexa-resilience/` (ветка `telemost-resilience`), **не** из `vexa/`: основное дерево грязное от
соседних планов, а в его ветке `notary-f2-bot-notarius-full` лежат четыре коммита «авторства
реплик» (план `2026-09-25-speaker-attribution-fix`), на выкатку которых слова не было. Цели
Makefile берут дерево из переменных — переопределяем их.

```bash
cd ~/Projects/meeting-notary
TREE=vexa-resilience
SHA=$(git -C $TREE rev-parse --short HEAD)
```

Команды идут отдельными вызовами оболочки (так работает агент) — переменные и папка между ними
**не сохраняются**: начинай каждый блок с этих трёх строк, иначе `$TREE` и `$SHA` окажутся пустыми
и команды упадут или возьмут не ту папку.

## 0. Перед началом

1. **Слово владельца** «выкатывай» в этом чате (правило проекта: прод бота — только интерактивом).
2. **Дерево чистое, коммит известен:** `git -C $TREE status --short` — пусто; `echo $SHA`
   записать (понадобится для отката и журнала).
3. **Окно без встреч** — ни одного бота в комнате и ни одной встречи в ближайшие 90 минут
   (есть строки в выводе — ждать окна; heredoc ниже — без отступов, иначе `EOF` не сработает):

```bash
ssh meeting-notary bash -s <<'EOF'
docker ps --filter name=vexa-notarius --format '{{.Names}}'
cd /srv/meeting-notary/vexa && PYTHONPATH=scripts MEETING_NOTARY_REGISTRY_DIR=/srv/meeting-notary/registry \
  /home/dev/meeting-notary/venv/bin/python - <<'PY'
from datetime import datetime, timedelta, timezone
from notary.lib.state import read_queue
now = datetime.now(timezone.utc)
for it in read_queue():
    s = datetime.fromisoformat(str(it.get("start_at")).replace("Z", "+00:00"))
    s = s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if now - timedelta(hours=2) <= s <= now + timedelta(minutes=90):
        print("ВСТРЕЧА", it.get("series") or it.get("meeting_id"), s.isoformat())
PY
EOF
```

4. **Что изменится на сервере — сверить заранее** (сравнение по содержимому, ничего не пишет):
   ```bash
   rsync -avnc --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '.*' \
     $TREE/scripts/notary/ meeting-notary:/srv/meeting-notary/vexa/scripts/notary/ | grep -v '/$'
   rsync -avnc --delete --exclude '__pycache__' \
     $TREE/services/vexa-bot/core/src/ meeting-notary:/home/dev/meeting-notary/vexa/services/vexa-bot/core/src/ | grep -v '/$'
   git -C $TREE diff --name-only 1851f95 HEAD -- scripts/notary services/vexa-bot/core/src   # что меняла ветка
   ```
   В выводе rsync должны быть **только** файлы из списка `git diff`. Есть строка `deleting …` или
   файл, которого ветка не меняла, — **стоп**: на сервере лежит код, которого нет в ветке (или
   старее её основы), и выкатка его откатит либо заодно включит чужое. Показать список владельцу;
   если это «авторство реплик» и владелец дал слово выкатить и его — сначала
   `git -C $TREE merge notary-f2-bot-notarius-full` (пробное слияние 26.09 — без конфликтов),
   прогнать набор тестов и повторить сверку.
5. **Сколько звука уберёт первый тик сборщика — показать владельцу до шага 3.** С шага 3 сборщик
   после доставки удаляет весь звук встречи (решение владельца 26.09, копилка №3), и первый же
   его тик (≤ 5 минут) уберёт куски 2…N и второе ухо у **всех уже доставленных** встреч, чья meta
   лежит в папке записей. Это **необратимо**: откат скриптов удалённое не вернёт. Верхняя граница
   (сюда входят и ещё не доставленные встречи, и серии с `keep_audio` — их звук останется):
   ```bash
   ssh meeting-notary 'cd /home/dev/meeting-notary/_tmp/transcripts && find . -maxdepth 1 -name "*.chunk*.wav" | wc -l && find . -maxdepth 1 -name "*.chunk*.wav" -print0 | du -ch --files0-from=- | tail -1'
   ```
   Число файлов и объём — владельцу одной строкой. Сказал «старые не трогать» — стоп, шаг 3 не
   делать: сперва решить, куда перенести старые куски.

## 1. Снимок для отката

Ровно один раз. Повтор после шага 3 или 6 записал бы в снимок уже новый код и новый образ — откатывать
стало бы не на что; поэтому команда сама отказывается, если снимок уже есть. Отказалась, а шаги 3 и 6
ещё не делались (прошлая попытка оборвалась раньше) и в выводе оба архива и тег — снимок годен, иди
к шагу 2; чего-то из трёх нет — стоп, показать владельцу.

```bash
ssh meeting-notary bash -s <<'EOF'
set -e
B=/srv/meeting-notary/.deploy-backup; sudo mkdir -p $B
if [ -e $B/before-iss60-scripts.tgz ] || docker image inspect vexa-bot:notarius-telemost-before-iss60 >/dev/null 2>&1; then
  echo "СНИМОК УЖЕ ЕСТЬ — не перезаписываю"; ls -la $B/before-iss60-* || true; docker image ls vexa-bot:notarius-telemost-before-iss60; exit 1
fi
sudo tar czf $B/before-iss60-scripts.tgz -C /srv/meeting-notary/vexa/scripts notary
sudo tar czf $B/before-iss60-botsrc.tgz -C /home/dev/meeting-notary/vexa/services/vexa-bot core/src core/entrypoint.sh
docker tag vexa-bot:notarius-telemost vexa-bot:notarius-telemost-before-iss60
ls -la $B/before-iss60-*; docker image ls vexa-bot --format '{{.Tag}} {{.ID}}' | head
EOF
```

## 2. Образ-кандидат (боевой ещё не тронут)

Цель выкатки скриптов везёт только `core/src`, а ветка поменяла и `core/entrypoint.sh` (`exec
node` — без него сигнал остановки до бота не доходит): его везём отдельно. Цель сборки образа
сама берёт номер сборки из `vexa/` — задаём его явно.

```bash
rsync -av --delete --exclude '__pycache__' \
  $TREE/services/vexa-bot/core/src/ meeting-notary:/home/dev/meeting-notary/vexa/services/vexa-bot/core/src/
rsync -av $TREE/services/vexa-bot/core/entrypoint.sh \
  meeting-notary:/home/dev/meeting-notary/vexa/services/vexa-bot/core/entrypoint.sh
make build-bot TAG=notarius-telemost-iss60 GIT_SHA=$SHA
ssh meeting-notary "docker run --rm --entrypoint sh vexa-bot:notarius-telemost-iss60 -c 'echo \$NOTARY_BOT_GIT_SHA; grep -c \"exec node\" /app/entrypoint.sh'"
```
Ждём `$SHA` и `1`. Сборка упала (проверка типов) — дальше не идём, на бою ничего не менялось.

## 3. Скрипты

```bash
make deploy-notary LOCAL_NOTARY_DIR=$TREE/scripts/notary LOCAL_VEXA_BOT_SRC=$TREE/services/vexa-bot/core/src
```
Цель везёт `scripts/notary` (с копией заменённого в `.deploy-backup/notary-<время>/`), заново
везёт `core/src` (то же содержимое) и перезапускает слушателя и шлюз. С этой минуты сторож входа
работает — пока со старым образом, в осторожном режиме. **И с этой же минуты сборщик убирает звук
доставленных встреч целиком:** первый тик (≤ 5 минут) удалит куски 2…N и второе ухо всех уже
доставленных встреч (объём — §0 п.5). Необратимо; расшифровки, протоколы, meta, звук под
удержанием портала и у серий или нарядов портала с `keep_audio` не трогаются.

## 4. Настройки в `/srv/meeting-notary/.env.notary`

Файл с секретами: не печатать его целиком, только `grep` по именам.

1. **Папки замков ручной финализации.** Обёртка восстановления держит замок встречи там же, где
   его ищут сборщик и повторы. Где замки у сборщика — видит только его юнит:
   ```bash
   ssh meeting-notary 'systemctl show meeting-notary-collector -p Environment | tr " " "\n" | grep STATE_DIR; grep -E "^MEETING_NOTARY_STATE_DIR=" /srv/meeting-notary/.env.notary'
   ```
   Нашлось значение — это папка сборщика; пусто — у сборщика умолчание
   `/home/dev/Library/Logs/meeting-notary`. Прописываем **обе** папки — сборщика и повторов
   (`/srv/meeting-notary/state`): явный список перебивает все умолчания.
   ```bash
   ssh meeting-notary "echo 'MEETING_NOTARY_FINALIZE_LOCK_DIRS=<папка сборщика>:/srv/meeting-notary/state' | sudo tee -a /srv/meeting-notary/.env.notary >/dev/null"
   ```
2. **Тестовых флагов и выключателей нет** (иначе сообщения людям уйдут владельцу, бот пойдёт
   запасным путём, ежедневная самопроверка будет гонять тестовый образ или чужую комнату, а сторож
   или предупреждения молча выключены). Это флаги, не секреты — строки печатаются целиком:
   ```bash
   ssh meeting-notary "grep -E '^(MEETING_NOTARY_JOIN_ALERT_TO_OWNER|MEETING_NOTARY_CANARY_FORCE_FALLBACK|MEETING_NOTARY_CANARY_FORCE_DEAF|MEETING_NOTARY_CANARY_IMAGE|MEETING_NOTARY_CANARY_ROOM|MEETING_NOTARY_JOIN_WATCH|MEETING_NOTARY_CANARY_WARN)=' /srv/meeting-notary/.env.notary"
   ```
   Ждём пустой вывод (допустимо только `…_JOIN_WATCH=1` / `…_CANARY_WARN=1`). Есть строка — показать
   владельцу и с его слова удалить (`sudo sed -i '/^ИМЯ=/d' /srv/meeting-notary/.env.notary`).
3. **Сторож видит docker локально** (иначе он молчит — так задумано для мака):
   ```bash
   ssh meeting-notary "systemctl show meeting-notary-runner -p Environment | grep -c 'MEETING_NOTARY_LOCAL_DOCKER=1'; grep -c '^MEETING_NOTARY_LOCAL_DOCKER=1' /srv/meeting-notary/.env.notary"   # хотя бы одна 1
   ```

## 5. Самопроверка кандидата в Т-10

Юниты ставим командами ниже (справка по ним — раздел «Самопроверка бота в Т-10…» выше), **без**
включения таймеров:
```bash
ssh meeting-notary bash -s <<'EOF'
sudo -u dev mkdir -p /srv/meeting-notary/state/canary /home/dev/meeting-notary/_tmp/canary /home/dev/meeting-notary/logs
grep -c "name: t10" /srv/meeting-notary/registry/rooms.yaml
cd /srv/meeting-notary/vexa
sudo cp scripts/notary/deploy/meeting-notary-canary.{service,timer} scripts/notary/deploy/meeting-notary-telemost-sniff.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo -u dev bash -c 'set -a; . /srv/meeting-notary/.env.notary; set +a; cd /srv/meeting-notary/vexa; \
  PYTHONPATH=scripts MEETING_NOTARY_LOCAL_DOCKER=1 MEETING_NOTARY_REGISTRY_DIR=/srv/meeting-notary/registry \
  MEETING_NOTARY_CANARY_STATE_DIR=/srv/meeting-notary/state/canary MEETING_NOTARY_CANARY_DIR=/home/dev/meeting-notary/_tmp/canary \
  /home/dev/meeting-notary/venv/bin/python -m notary.canary run --reason manual --image vexa-bot:notarius-telemost-iss60'
EOF
```
Итог прогона — в выводе: **зелёный** → дальше. `skipped` (в Т-10 кто-то есть) → повторить позже.
Жёлтый или красный → **образ не переключать**, шаги 6–7 пропустить; скрипты можно оставить (они
совместимы со старым образом) или откатить (ниже). **Оставил скрипты — шаг 8 всё равно сделать:**
инструкции Альфреда зависят от скриптов, а не от образа (новый сторож советует людям «скажи
Альфреду „обработай запись“», новая обёртка ждёт `--mode auto`). Откатил скрипты — шаг 8 не делать.
Прогон кандидата боевое состояние самопроверки и сообщения владельцу не трогает.

## 6. Переключить боевой образ

```bash
ssh meeting-notary 'docker tag vexa-bot:notarius-telemost-iss60 vexa-bot:notarius-telemost && docker image ls vexa-bot'
```
Следующий запуск бота — уже новым образом; ботов, которые сейчас в комнатах, это не трогает.

## 7. Первый прогон на боевом образе и таймеры

```bash
ssh meeting-notary bash -s <<'EOF'
sudo systemctl start meeting-notary-canary.service; journalctl -u meeting-notary-canary -n 20 --no-pager
cd /srv/meeting-notary/vexa && sudo -u dev env PYTHONPATH=scripts MEETING_NOTARY_CANARY_STATE_DIR=/srv/meeting-notary/state/canary \
  /home/dev/meeting-notary/venv/bin/python -m notary.canary status
EOF
```
`start` ждёт конца прогона (обычно 5–10 минут, с перепроверкой красного — до 20). «Бот здоров» →
включаем таймеры и один раз «нюх», чтобы он запомнил текущую версию Телемоста:
```bash
ssh meeting-notary 'sudo systemctl enable --now meeting-notary-canary.timer meeting-notary-telemost-sniff.timer && sudo systemctl start meeting-notary-telemost-sniff.service && systemctl list-timers "meeting-notary-*"'
```
Если в папке состояния осталась версия Телемоста от тестовых прогонов, «нюх» увидит «смену»:
владельцу — тихая строка «Телемост обновился… бота проверю утром», проверку закроет ближайшая
ежедневная самопроверка в 04:40 МСК (или внеплановая в 07:00–08:00). Днём «нюх» самопроверку не
запускает — это нормально (`python -m notary.canary status` покажет «ждёт проверки бота утром»).

Итог не «бот здоров»:
- `skipped` (в Т-10 кто-то есть или скоро встреча) → повторить `start` позже, таймеры пока не включать;
- `red_unconfirmed` (первый прогон красный, перепроверить сразу не вышло) → повторить `start` вручную:
  днём «нюх» перепроверку не делает, она ждала бы утра 07:00–08:00;
- красный → таймеры **не** включать, откатить образ и выключить самопроверку (§10, первые две строки:
  красный итог на боевом образе уже включил предупреждения перед встречами), показать владельцу вывод
  `status`. Скрипты и шаг 8 — как в шаге 5;
- жёлтый (бот входит, но что-то второстепенное: имена, вход запасным путём, сбой расшифровки у самой
  проверки) → таймеры пока не включать, показать владельцу причину из `status` и ждать его слова.

## 8. Инструкция Альфреда и методичка восстановления

Версии к выкатке лежат в ветке (`scripts/notary/docs/rollout/`); живые на маке — прежние.
Заменить целиком, снять вводный блок «Версия к выкатке…», закоммитить каждый поимённо:
```bash
cp $TREE/scripts/notary/docs/rollout/alfred-notary-recovery-from-recording.md ~/.claude/methods/telegram-daemon/notary-recovery-from-recording.md
cp $TREE/scripts/notary/docs/rollout/recover-meeting-protocol-from-recording.md ~/Projects/me/methods/recover-meeting-protocol-from-recording.md
sed -i '' '/^> \*\*Версия к выкатке ISS-60\*\*/,/^$/d' ~/.claude/methods/telegram-daemon/notary-recovery-from-recording.md ~/Projects/me/methods/recover-meeting-protocol-from-recording.md
git -C ~/.claude commit -m "Альфред: восстановление протокола из записи — версия ISS-60 (выкатка)" -- methods/telegram-daemon/notary-recovery-from-recording.md
git -C ~/Projects/me commit -m "Методичка восстановления протокола — версия ISS-60 (выкатка)" -- methods/recover-meeting-protocol-from-recording.md
```
Строка маршрутизации Альфреда (`~/.claude/channels/telegram/daemon-cwd/CLAUDE.md`, абзац
«Восстановление протокола из присланной записи») после выкатки расходится с методом дважды: ведёт в
метод только по «файл + просьба о протоколе» (фраза «обработай запись» без файла туда не попадёт) и
утверждает «доставку делает обёртка, ЛИЧНО Илье», а метод теперь запускает `--mode auto` (серия с
группой, подтверждённая журналом сторожа или календарём, → в чат серии). «Ок» владельца на правку
этой строки есть (26.09, копилка плана, №4) — поправить оба места здесь же, коммит поимённо.

## 9. Что смотреть после

- **Сразу:** `journalctl -u meeting-notary-runner --since "15 min ago" --no-pager | grep -E "join-watch|Traceback"` —
  без `Traceback`; `systemctl is-active meeting-notary-listener meeting-notary-gateway` — оба `active`.
- **Первый тик сборщика:** он уберёт куски записи 2…N уже доставленных встреч, чья meta ещё лежит в
  папке записей (решение владельца 26.09, копилка №3: звук после доставки удаляется целиком, как
  первый кусок; расшифровки и протоколы не трогаются) — в журнале сборщика строки «Кусок записи
  удалён после доставки». Под удержанием портала и у серий или нарядов портала с `keep_audio`
  (флаг в реестре серий / в наряде) звук остаётся.
- **Первая боевая встреча:** в журнале запускателя `[join-watch] …: вход подтверждён`; статус входа
  `/home/dev/meeting-notary/_tmp/transcripts/.join-status/<sessionUid>.json` — `in_call`; в `meta.json`
  встречи есть участники (хвост ISS-59: имена); протокол пришёл с именами.
- **Следующее утро:** самопроверка в 04:40 МСК — зелёная (`python -m notary.canary status`), «нюх» —
  без ошибок (`journalctl -u meeting-notary-telemost-sniff -n 20`).
- **Через сутки:** все службы живы (§7 выше) — одна реальная встреча записалась, протокол пришёл.
  Протоколы составляет Claude подписки бота: пока её недельный лимит исчерпан (ISS-61), протокол
  не соберётся независимо от этой выкатки — расшифровка сохранится, сборка повторится.
- **Первое «обработай запись» после выкатки:** живая доставка восстановленного протокола и правка
  ответом на нём до выкатки не проверены (упёрлись в лимит Claude, ISS-61) — первый реальный случай
  досмотреть глазами: протокол пришёл туда, куда сказал Альфред, правка ответом сработала. Не
  сработала — ISS в журнал косяков.
- **Документы в ту же сессию:** `docs/how-it-works.md` — раздел 7 из «готово к выкатке» в «в бою» (с
  датой и что не подтвердилось), запись журнала с отметкой выкатки, пометки «⏭ после выкатки» у
  записей 30.06; `plans/notary-issues-log.md` — статус ISS-60; фаза 7 плана; строка штурману в
  `plans/compass-inbox.md`.
- **Слияние:** когда в `vexa/` никто не работает (`git -C vexa status --short` без чужих правок) —
  `git -C vexa merge telemost-resilience` в `notary-f2-bot-notarius-full` (план: «слияние в конце»).
- **Уборка тестового на сервере — после приёмки** (всё создано планом ISS-60 для живых проверок,
  боевое не трогает): тестовые образы `vexa-bot:resil-*`, папки `/home/dev/resil-build/` (их
  сборка) и `/home/dev/resil-test/` (тестовая копия скриптов и песочница проверки восстановления).
  `resil-test` — **не раньше**, чем повторена живая проверка восстановленного протокола (фаза 5
  плана, `tools/recovery-live-check.sh` запускается оттуда). Сначала список, потом удаление:
  ```bash
  ssh meeting-notary 'docker image ls vexa-bot --format "{{.Repository}}:{{.Tag}}" | grep ":resil-"; du -sh /home/dev/resil-build /home/dev/resil-test'
  ssh meeting-notary 'docker image ls vexa-bot --format "{{.Repository}}:{{.Tag}}" | grep ":resil-" | xargs -r docker image rm && rm -rf /home/dev/resil-build /home/dev/resil-test'
  ```
  Теги отката (`notarius-telemost-before-iss60`, `notarius-telemost-pre3-20260923`) не трогать.

## 10. Как откатить (каждую часть отдельно)

| Что | Как | Когда |
|---|---|---|
| Образ бота | `ssh meeting-notary 'docker tag vexa-bot:notarius-telemost-before-iss60 vexa-bot:notarius-telemost'` + **обязательно выключить самопроверку** (строка ниже) — на старом образе она красная | бот не входит / входит хуже прежнего |
| Самопроверка и предупреждения | `sudo systemctl disable --now meeting-notary-canary.timer meeting-notary-telemost-sniff.timer` + строка `MEETING_NOTARY_CANARY_WARN=0` в `.env.notary` | ложные тревоги, мешает |
| Только сторож входа | строка `MEETING_NOTARY_JOIN_WATCH=0` в `.env.notary` — со следующего тика запускателя сторож молчит, остальное работает. Замолкают **все** его сообщения: о сорванном входе, «впустите бота», «выкинуло» и «перестал слышать» посреди встречи (сам бот в Телеграм не пишет — токена в контейнере нет), повторов входа и «бот не пришёл» порталу тоже не будет | сторож шлёт лишнее людям |
| Скрипты целиком | `ssh meeting-notary 'sudo rm -rf /srv/meeting-notary/vexa/scripts/notary && sudo tar xzf /srv/meeting-notary/.deploy-backup/before-iss60-scripts.tgz -C /srv/meeting-notary/vexa/scripts && sudo chown -R dev:dev /srv/meeting-notary/vexa/scripts/notary && sudo systemctl restart meeting-notary-listener meeting-notary-gateway'` + выключить самопроверку (её скриптов больше нет) + **откатить и образ, если он уже новый**: второе ухо нового образа пишет файл записи, который старые скрипты после доставки не удаляют, — голоса людей копились бы на диске. Звук встреч, удалённый после шага 3, откат **не вернёт** | ошибки в журналах служб после выкатки |
| Исходники образа (для пересборки прежнего) | `ssh meeting-notary 'cd /home/dev/meeting-notary/vexa/services/vexa-bot && rm -rf core/src && tar xzf /srv/meeting-notary/.deploy-backup/before-iss60-botsrc.tgz'` (сначала убрать `core/src`: иначе новые файлы ветки останутся рядом со старыми и сборка возьмёт смесь), затем `make build-bot TAG=notarius-telemost-before-iss60 GIT_SHA=1851f95` и первая строка таблицы | нужен прежний образ, а тег отката потерян |
| Замки ручной финализации | удалить строку `MEETING_NOTARY_FINALIZE_LOCK_DIRS=…` из `.env.notary` — замок вернётся к папкам по умолчанию | не должно понадобиться |
| Инструкция Альфреда / методичка | `git -C ~/.claude revert <коммит>` / `git -C ~/Projects/me revert <коммит>` (коммиты шага 8) | откатили скрипты — откатить и их: они ссылаются на `--mode auto` новой обёртки |

Правка `.env.notary` для отката — тем же `sudo tee -a` (добавить строку) или `sudo sed -i '/^ИМЯ=/d'`
(удалить); таймерные службы читают файл на каждом запуске, слушатель и шлюз — после
`sudo systemctl restart meeting-notary-listener meeting-notary-gateway`.
