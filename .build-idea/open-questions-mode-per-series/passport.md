# passport.md — среда, гейты, базовая линия

Снят: 2026-08-04 20:05. Единственный источник команд для всех агентов прогона.

## Среда

| Что | Значение |
|---|---|
| Репозиторий | vexa (форк), worktree `sandbox-build-idea/open-questions-mode-per-series` |
| Ветка | `build-idea/open-questions-mode-per-series` |
| Зона работ | `scripts/notary/` (код бота-нотариуса) |
| Язык | Python |
| `python3` | 3.9.6 — `/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python3` (первый в PATH) |
| Тестовый фреймворк | `unittest` из stdlib. **pytest в среде отсутствует и не нужен** |
| Тесты | `scripts/notary/tests/`, 107 файлов `test_*.py`, каждый самостоятелен (`__main__` + `unittest.main()`) |
| Зависимости | `scripts/notary/requirements.txt` (requests, soundfile, torch…) — для тестов зоны не требуются |

## Гейт зоны (единственный автоматический)

```sh
sh .build-idea/open-questions-mode-per-series/gate.sh          # всё
sh .build-idea/open-questions-mode-per-series/gate.sh 'test_iss5*'   # маска
```
Код возврата 0 — зелёный, 1 — есть падения. Вывод каждого упавшего файла — последние 25 строк.

## 🔴 Находка этапа 0: постоянный CI не покрывает зону

`.github/workflows/` содержит `chart-release`, `test-admin-api`, `test-api-gateway`,
`test-meeting-api`, `test-packages` — **ни один не запускает `scripts/notary/tests`**.
`Makefile` репозитория слова `notary` не содержит вовсе; `Makefile` проекта-обёртки содержит только
цели деплоя. Хук `pre-commit` — lefthook, тесты зоны не гоняет.

Следствие по §4 ворот сдачи: гейт проекта мою зону не покрывает, мутационная проверка опирается на
раннер, заведённый этим прогоном. Это строка владельцу, а не повод считать мутации пройденными.

## Базовая линия ДО первой правки (артефакт: `artifacts/baseline-gate.txt`)

**Прогнано 107 файлов, красных 3.** Базовая линия проекта красная сама по себе — проверено, что те же
три файла красные и в основном рабочем дереве (`meeting-notary/vexa/`), то есть это не эффект песочницы
и не мои поломки. От каталога запуска не зависят (проверено запуском из `scripts/notary/` и из `tests/`).

Карантин — 8 поимённых проверок, красных ДО меня:

| Файл | Проверка |
|---|---|
| `test_iss33_mixed_reply_split.py` | `TestSplitModule.test_stub_split_fn_used_no_real_claude` |
| `test_phase6_journal.py` | `TestVocativeJournalViaContour.test_journal_off_by_default` |
| `test_phase6_journal.py` | `TestVocativeJournalViaContour.test_journal_on_when_flag_set` |
| `test_phase6_journal.py` | `TestVocativeJournalViaContour.test_plaque_still_written_without_journal` |
| `test_watson_phase4_morning_digest.py` | `TestListenerWiring.test_tick_grid_matches_timer` |
| `test_watson_phase4_morning_digest.py` | `TestListenerWiring.test_systemd_units_exist` |
| `test_watson_phase4_morning_digest.py` | `TestCycleGuards.test_state_write_failure_is_reported` |
| `test_watson_phase4_morning_digest.py` | `TestSendGuards.test_logs_carry_no_task_text` |

**Правило чтения гейта до конца прогона:** зелёным считается прогон, где множество красных проверок —
подмножество этого карантина. Любая проверка вне карантина = моя поломка.

⚠️ `test_watson_phase4_morning_digest.py` лежит прямо в зоне Фазы 3 (`morning_digest`). Часть его
падений — от путей песочницы (юниты systemd ищутся по пути от корня репозитория, а живут в обёртке),
часть настоящие. Наблюдалась нестабильность набора красных между прогонами — при работе с Ф3 сверять
поимённо, а не по числу.

## Как откатить прогон

`возврат версии` — каждая фаза закрывается отдельным коммитом в ветке прогона, точка отката = коммит
предыдущей фазы. Данные прогон не меняет: боевой реестр и прод под парковкой.
