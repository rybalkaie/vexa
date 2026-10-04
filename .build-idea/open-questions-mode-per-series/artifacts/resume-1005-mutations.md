# Мутационная проверка починенных тестов и герметичности (05.10.2026, ломатель)

Копия для мутаций: `/tmp/oq-resume-IeQqPZ/mutants` (архив HEAD 8953a85d; `scripts/notary` в нём совпадает с нынешним HEAD 30a18832). Рабочее дерево не тронуто, кроме этого отчёта.
Оракул: `sh .build-idea/open-questions-mode-per-series/gate.sh '<маска>'` копии. Перед каждым прогоном — `PYTHONDONTWRITEBYTECODE=1` и проверка, что `__pycache__` в `scripts/notary` копии нет (почему не снос — см. оговорку в п. 0).

(отчёт завершён; итоговая строка — в конце)

## 0. Эталон до мутаций

- Копия `scripts/notary` совпадает с рабочим деревом побайтно (`diff -rq` пуст); чистые копии четырёх файлов, которые ломаю, отложены в `/tmp/oq-resume-IeQqPZ/breaker-orig-1005/`.
- `gate.sh 'test_iss53_55_evening_reject_tz.py'` → rc=0, ЗЕЛЁНЫЙ, вызовов claude 0.
- `gate.sh 'test_phase7_series_memory.py'` → rc=0, ЗЕЛЁНЫЙ, вызовов claude 0.
- Прямой прогон `-v`: `test_apply_survives_broken_element_and_applies_the_rest` и `test_prune_runs_after_save` реально исполняются и проходят (в логе первого виден KeyError 'event_id' — это и есть путь «битый элемент», пойманный поэлементно).
- Оговорка по процедуре: снос `__pycache__` командой `rm -r` запрещён правами окна. Вместо этого перед каждым прогоном проверяю `find … -name __pycache__ | wc -l` = 0, а `PYTHONDONTWRITEBYTECODE=1` не даёт кэшу появиться. Кэша не было ни разу (числа в пунктах ниже).

## М1. `meetings_apply_reply.apply_decisions` — исправное решение при битом соседе

Охраняющий тест: `test_iss53_55_evening_reject_tz.py::TestBrokenSnapshotItems::test_apply_survives_broken_element_and_applies_the_rest`. В тесте решения идут в порядке «n=1 исправный, n=2 битый (нет event_id)»; битый падает KeyError внутри `make_record_id`.

### М1а — битый элемент роняет весь ответ (состояние «до ремонта 5») — ПОЙМАНА

- Правка: `meetings_apply_reply.py:746`, в поэлементном `except Exception` после `skipped.append(...)` вместо `continue` поставлен `raise`.
- Команда: `PYTHONDONTWRITEBYTECODE=1 sh .build-idea/open-questions-mode-per-series/gate.sh 'test_iss53_55_evening_reject_tz.py'` (кэш: 0).
- Код возврата: 1, КРАСНЫЙ. Упала ровно одна проверка: `ERROR: test_apply_survives_broken_element_and_applies_the_rest`.
- Откат: файл возвращён из чистой копии, `cmp` с рабочим деревом — идентичен; эталон снова rc=0, ЗЕЛЁНЫЙ.

### М1б — цикл решений обрывается на первом битом (`break`) — ВЫЖИЛА (главная находка)

- Правка: `meetings_apply_reply.py:746`, в том же `except` вместо `continue` поставлен `break` — ровно пример из задания «прервать цикл решений на первом битом».
- Команда: `gate.sh 'test_iss53_55_evening_reject_tz.py'` (кэш: 0) → rc=0, ЗЕЛЁНЫЙ, упавших проверок нет.
- Расширил оракул на все три файла зоны, где зовут `apply_decisions`: `gate.sh 'test_clean_slate_and_rejection_guards.py test_iss53_55_evening_reject_tz.py test_iss54_meeting_dedup.py'` → rc=0, ЗЕЛЁНЫЙ. Ни один из этих трёх файлов мутанта не видит. Полный гейт зоны на этом мутанте не гонял: остальные файлы `apply_decisions` не зовут.
- Почему тест её не видит: в тесте исправное решение (n=1) стоит ПЕРЕД битым (n=2). `collapse_decisions` сохраняет порядок ответа, исправное применяется до того, как цикл дойдёт до битого, а `break` после битого терять уже нечего. Все четыре утверждения теста (`rejected == 1`, одна запись, одно сохранение, в `skipped` есть «2:») выполняются. Имя теста обещает «applies_the_rest», но «остальное» в нём только ДО битого элемента.
- Доказательство пробой (тестовые файлы не менял; хелпер `_apply` и данные взяты из самого тестового модуля, скрипт `/tmp/oq-resume-IeQqPZ/breaker-orig-1005/probe_m1_order.py`):
  - мутант, порядок как в тесте: `rejected=1 watched=1 saved=1`;
  - мутант, битый первым: `rejected=0 watched=0 saved=0` — исправное «нет» владельца потеряно, и молча: его номера нет ни в `skipped`, ни в `no_answer`, ни в строках для владельца;
  - эталон, оба порядка: `rejected=1 watched=1 saved=1`. Код верен, дыра только в тесте.
- Как закрыть (предложение, не делал): прогнать тот же сценарий и с порядком решений «битый первым», например `[{"n": 2, …}, {"n": 1, …}]`, или параметризовать тест по обоим порядкам.
- Насколько это важно на бою: `main()` до `apply_decisions` отсеивает битые элементы через `sanitize_snapshot_items`, поэтому KeyError по event_id на бою сюда не доходит. Поэлементный `except` остаётся второй линией защиты от любых других исключений на отдельном решении (`_mute_twins`, дедуп, сверка с реестром по кривой строке `watched.yaml`). Регресс `continue → break` там тихо выбросил бы все решения после первого упавшего, а все три файла про `apply_decisions` остались бы зелёными.
- Откат: файл возвращён, `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ; проба на эталоне — оба порядка дают `rejected=1`.

### М1в — битый сосед отменяет весь ответ, ничего не сохраняется — ПОЙМАНА

- Правка: `meetings_apply_reply.py:746`, в `except` вместо `continue` — `return {"error": …, "rejected": 0, …, "skipped": skipped}` без `save_watched` (fail-closed на весь ответ: «есть битый — не применяю ничего»).
- Команда: `gate.sh 'test_iss53_55_evening_reject_tz.py'` (кэш: 0) → rc=1, КРАСНЫЙ.
- Упала одна проверка: `FAIL: test_apply_survives_broken_element_and_applies_the_rest` с сообщением теста «0 != 1 : исправное решение владельца потеряно».
- Откат: `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ.

### М1г — отказ по исправной встрече тихо выпадает из реестра, а счётчик врёт «записал» — ПОЙМАНА

- Правка: `meetings_apply_reply.py:746–750`, в `except` перед `continue` — «откат» уже записанных отказов: из `data["watched"]` выкидываются записи с `created_by_rejection`. Счётчик `rejected` и строка «больше не спрошу» остаются, сохранение идёт.
- Команда: `gate.sh 'test_iss53_55_evening_reject_tz.py'` (кэш: 0) → rc=1, КРАСНЫЙ.
- Упала одна проверка: `FAIL: test_apply_survives_broken_element_and_applies_the_rest`, `AssertionError: 0 != 1` (утверждение `len(data["watched"]) == 1`). Тест проверяет не только счётчик, но и сам реестр, и поэтому ловит такую «ложь владельцу».
- Откат: `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ.

## М2. `lib/series_memory.save_meeting_digest` без уборки после сохранения — ПОЙМАНА

- Правка: `lib/series_memory.py:1373–1374`, убраны строки `if prune_days and prune_days > 0: prune_old_digests(series_dir, prune_days)`; функция только сохраняет.
- Команда: `PYTHONDONTWRITEBYTECODE=1 sh .build-idea/open-questions-mode-per-series/gate.sh 'test_phase7_series_memory.py'` (кэш: 0) → rc=1, КРАСНЫЙ.
- Упала одна проверка: `FAIL: test_prune_runs_after_save (TestSaveMeetingDigestB1)`, `AssertionError: True is not false` — старая выжимка `2020-01-01` осталась на месте.
- Откат: `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ.

## М3. `lib/series_memory.prune_old_digests` сносит все выжимки, включая свежие — ПОЙМАНА

- Правка: `lib/series_memory.py:1461`, `if date < cutoff:` заменено на `if True:` — сравнение с порогом всегда истинно.
- Команда: `gate.sh 'test_phase7_series_memory.py'` (кэш: 0) → rc=1, КРАСНЫЙ.
- Упали три проверки: `test_prune_runs_after_save` («False is not true» — свежая сегодняшняя выжимка снесена тем же вызовом, что её записал), `test_prune_removes_old_keeps_recent` («3 != 1»), `test_prune_root_across_series_skips_service` («3 != 2»). Охраняемый тест в списке.
- Откат: `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ.

### М3б (дополнительно) — свежие сносятся только на боевом пути, где `today` не передан — ПОЙМАНА

- Зачем: все остальные тесты уборки передают `today` явно; боевой путь `save_meeting_digest → prune_old_digests(series_dir, prune_days)` без `today` проверяет только починенный тест. Хотел убедиться, что после замены зашитой даты на `date.today()` он этот путь по-прежнему стережёт в одиночку.
- Правка: `lib/series_memory.py:1447`, в ветке `if today is None:` вместо `_date.today().isoformat()` — `"9999-12-31"`, то есть порог уходит в будущее и уборка сносит всё.
- Команда: `gate.sh 'test_phase7_series_memory.py'` (кэш: 0) → rc=1, КРАСНЫЙ.
- Упала ровно одна проверка: `FAIL: test_prune_runs_after_save`, «False is not true» — свежая выжимка снесена. Тесты с явным `today` зелёные, как и ожидалось: этот путь стережёт только починенный тест, и он его стережёт.
- Откат: `cmp` — идентичен; эталон rc=0, ЗЕЛЁНЫЙ.

## Календарная устойчивость (сдвиг часов, не мутация)

Подмена работает: под `PYTHONPATH=/tmp/oq-resume-IeQqPZ/clockshift CLOCK_SHIFT_DAYS=400` процесс видит `date.today() = 2027-11-09`, `datetime.now()` и `time.localtime()` — тоже 2027-11-09; модуль подмены грузится из `/tmp/oq-resume-IeQqPZ/clockshift/sitecustomize.py`. Без сдвига — 2026-10-05.

Эталон (починенные тесты) со сдвигом +400 суток, оракул — гейт копии:
- `PYTHONPATH=… CLOCK_SHIFT_DAYS=400 gate.sh 'test_iss53_55_evening_reject_tz.py'` → rc=0, ЗЕЛЁНЫЙ, claude 0.
- `PYTHONPATH=… CLOCK_SHIFT_DAYS=400 gate.sh 'test_phase7_series_memory.py'` → rc=0, ЗЕЛЁНЫЙ, claude 0.

Положительный контроль подмены — старые версии из `/tmp/oq-resume-IeQqPZ/mainline/scripts/notary/tests/`. Запускал напрямую `python3 <файл>` из этой папки. Для осторожности HOME подменял на пустую временную папку (`PYTHONUSERBASE` — на настоящую базу пакетов). Для второго файла ещё и ставил первой в PATH свою заглушку claude; первый файл модель не зовёт, в гейте у него 0 вызовов. На ход часов это не влияет.
- `test_iss53_55_evening_reject_tz.py` (без `today=`), `CLOCK_SHIFT_DAYS=-40` → rc=0, `Ran 59 tests … OK` — как и ожидалось.
- Тот же файл, `CLOCK_SHIFT_DAYS=0` → rc=1, `FAILED (failures=1)`: ровно `FAIL: test_apply_survives_broken_element_and_applies_the_rest`, «0 != 1». Ретеншн от настоящей даты стёр только что записанный отказ — мина сработала, как и ожидалось.
- Дополнительно, симметрично для второй мины: старый `test_phase7_series_memory.py` (зашитая «свежая» 2026-06-09) при `CLOCK_SHIFT_DAYS=0` → rc=0, OK; при `CLOCK_SHIFT_DAYS=400` → rc=1, ровно `FAIL: test_prune_runs_after_save`, «False is not true». Значит, подмена доходит и до логики уборки, а зелёный починенного файла на +400 — заслуга починки, а не того, что сдвиг «не доехал».
- Во временных HOME после всех четырёх прогонов — 0 файлов; вызовов заглушки claude — 0.

## Обратный контроль герметичности

Безопасность прогона: перед запуском проверил, что без изоляции запись уходит именно во временную HOME, а не в настоящие файлы. `DEFAULT_LOCAL_ROOT` в `lib/feedback_state.py:81` считается через `expanduser` от HOME; `/opt/meeting-notary` на этом маке нет; путь состояния подтверждений в `lib/reply_ack_state.py` — `~/.local/state/…` через `expanduser`; переменных `MEETING_NOTARY_*` в окружении нет; зашитые `/Users/…` в коде — только строки-шебанги. Каждый прогон шёл с `HOME=<своя пустая mktemp-папка>`, `PYTHONUSERBASE=/Users/ilarybalka/Library/Python/3.9`, в PATH первой стояла своя заглушка claude. С настоящей HOME эти файлы не запускались ни разу.

### `test_phase4_targeted_edit.py` (`MEETING_NOTARY_FEEDBACK_DIR`)

- Эталон, изоляция на месте: `python3 test_phase4_targeted_edit.py` из папки tests копии → rc=0, `Ran 45 tests … OK`; во временной HOME — пусто (0 файлов).
- Мутант H1: удалены `setUpModule`/`tearDownModule` (`tests/test_phase4_targeted_edit.py:617–629`). Тот же запуск → rc=0, `Ran 45 tests … OK`. Во временной HOME появились:
  - `~/Projects/meeting-notary/_feedback_edits/_learning/feedback-learning.jsonl`
  - `~/Projects/meeting-notary/_feedback_edits/_telemetry/patch-tiers.jsonl`
- Вывод: изоляция работает — без неё запись в журнал обучения и телеметрию уходит в домашнюю папку, с ней не уходит.
- Откат: `cmp` с рабочим деревом — идентичен; повторный запуск в новой пустой HOME → rc=0, 0 файлов.

### `test_watson_ph3_understand.py` (`MEETING_NOTARY_ACK_STATE_FILE`)

- Эталон, изоляция на месте: запуск из папки tests копии → rc=0, `Ran 45 tests … OK`; во временной HOME — пусто.
- Мутант H2: удалены `setUpModule`/`tearDownModule` (`tests/test_watson_ph3_understand.py:610–622`). Тот же запуск → rc=0, `Ran 45 tests … OK`. Во временной HOME появился `~/.local/state/meeting-notary-watson/reply-ack-state.json`.
- Вывод: изоляция работает — без неё пишется файл состояния слушателя, с ней не пишется.
- Откат: `cmp` — идентичен; повторный запуск в новой пустой HOME → rc=0, 0 файлов. Вызовов заглушки claude за все шесть прогонов обратного контроля — 0.

### Наблюдение: снятие изоляции гейт не заметит

В обоих мутантах H1 и H2 файл остаётся зелёным (rc=0), а `gate.sh` HOME не подменяет и записи в домашнюю папку не считает: вызовы claude он считает, записи в HOME — нет. Если кто-то уберёт `setUpModule`, гейт останется зелёным, а тесты снова начнут писать в настоящий `~/Projects/meeting-notary/_feedback_edits/…`. На сервере, где есть `/opt/meeting-notary`, это боевой журнал. Утечку сейчас ловит только ручной прогон по файлам (`hermeticity-per-file-2026-10-05.txt`). Предложение, не делал: пусть гейт гоняет файлы с временной HOME и печатает, какие файлы в ней появились, так же как он уже печатает вызовы claude.

## Дополнительно: перепроверка правки теста, сделанной по находке М1б

Пока я работал, в рабочем дереве появилась незакоммиченная правка `scripts/notary/tests/test_iss53_55_evening_reject_tz.py` (не моя; HEAD теперь 30a18832, правка поверх). Тест прогоняет сценарий в обоих порядках, `(1, 2)` и `(2, 1)`, через `subTest`. Я снял её копию (sha256 `a06505d79cbb0559…`; к концу проверки файл в дереве не менялся) и временно поставил в изолированную копию:
- эталонный код: `gate.sh 'test_iss53_55_evening_reject_tz.py'` → rc=0, ЗЕЛЁНЫЙ;
- мутант М1б (`continue → break`): rc=1, КРАСНЫЙ, `FAIL: test_apply_survives_broken_element_and_applies_the_rest (порядок=(2, 1))`, «0 != 1 : исправное решение владельца потеряно» — теперь ловится;
- эталонный код со сдвигом часов +400 суток: rc=0, ЗЕЛЁНЫЙ.
После этого в копию возвращена архивная версия теста (sha256 совпадает с версией в 8953a85d), код — исходный. В копии не осталось ни одной метки `MUTANT`, `__pycache__` нет. От рабочего дерева копия отличается только этой незакоммиченной правкой теста.

Служебное: логи всех прогонов, чистые копии файлов, проба порядка `probe_m1_order.py`, своя заглушка claude и временные HOME лежат в `/tmp/oq-resume-IeQqPZ/breaker-orig-1005/`. Удалить её не дают права окна (`rm -r` запрещён), поэтому оставил её как доказательную базу.

## Итог

поймано 6 из 7 мутаций (М1а, М1в, М1г, М2, М3, М3б; выжила М1б `continue → break` в поэлементном `except` у `apply_decisions` — тест проверял только порядок «исправное до битого»; правка теста из рабочего дерева с обоими порядками её уже ловит); обратный контроль: изоляция в обоих файлах работает — без `setUpModule` во временной HOME появляются `~/Projects/meeting-notary/_feedback_edits/_learning/feedback-learning.jsonl` + `…/_telemetry/patch-tiers.jsonl` и `~/.local/state/meeting-notary-watson/reply-ack-state.json`, с изоляцией — 0 файлов; но тесты при этом остаются зелёными, и гейт снятие изоляции не заметит; сдвиг часов: оба починенных файла зелёные при +400 суток, подмена доказана — старый первый тест OK при −40 и FAILED при 0, старый второй OK при 0 и FAILED при +400, каждый раз падает ровно охраняемая проверка.
