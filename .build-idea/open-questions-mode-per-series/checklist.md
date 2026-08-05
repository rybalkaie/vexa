# checklist.md

Формат: `- [ ]` открыт · `- [x] → путь` закрыт артефактом · `[x] SKIP/N/A/MANUAL: причина` · `[!] BLOCKED: причина`.
Гейт фазы: `python3 .claude/skills/build-idea/scripts/verify-checklist.py .build-idea/open-questions-mode-per-series/checklist.md --до "Фаза N" --корень .`

## Этап 0. Классификация и опора

- [x] Паспорт среды снят → .build-idea/open-questions-mode-per-series/passport.md
- [x] Базовая линия до первой правки прогнана → .build-idea/open-questions-mode-per-series/artifacts/baseline-gate.txt
- [x] Список несущих утверждений выложен → .build-idea/open-questions-mode-per-series/requirements.md
- [x] Гейт зоны заведён и запущен → .build-idea/open-questions-mode-per-series/gate.sh
- [x] Верификатор чек-листа проверен запуском → .build-idea/open-questions-mode-per-series/artifacts/verify-first-run.txt

## Этап 0.5. Разведка зоны

- [x] Карта зоны семью разведчиками → .build-idea/open-questions-mode-per-series/zone-map.md
- [x] Список файлов прогона → .build-idea/open-questions-mode-per-series/files.txt
- [x] Пересказ владельцу → .build-idea/open-questions-mode-per-series/owner-brief.md

## Фаза 1. Режим в реестре, чтение и знание своей серии

- [x] Поле open_questions_mode + валидация, неизвестное → дефолт last-only + предупреждение в лог → .build-idea/open-questions-mode-per-series/artifacts/f1-repair.md
- [x] Хелпер series_open_questions_mode с нормализацией ключа (ISS-7) → .build-idea/open-questions-mode-per-series/artifacts/f1-diff.txt
- [x] ISS-52: фолбэк резолва серии по chat_id текущего чата (одна → молча, несколько → переспрос) → .build-idea/open-questions-mode-per-series/artifacts/f1-traces.md
- [x] Тесты фазы написаны и зелёные → .build-idea/open-questions-mode-per-series/artifacts/gate-f1.txt
- [x] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/gate-f1.txt
- [x] След 1: прошёл путь как пользователь → .build-idea/open-questions-mode-per-series/artifacts/f1-traces.md
- [x] След 2: невалидные входы каждого перехода перечислены → .build-idea/open-questions-mode-per-series/artifacts/f1-traces.md
- [x] След 3: грепнул, где ещё этот же класс → .build-idea/open-questions-mode-per-series/artifacts/f1-traces.md
- [x] След 4: мерил на боевых объёмах → .build-idea/open-questions-mode-per-series/artifacts/f1-traces.md
- [x] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f1-review-1.md
- [x] Ревью 2 и 3 фазы 1 → .build-idea/open-questions-mode-per-series/artifacts/f1-review-3.md
- [x] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f1-mutation.md
- [x] Коммит фазы поимённо → f7271f0

## Фаза 2. Переключение режима командой боту

- [x] Узкий детерминированный триггер обеих сторон + форма с явным именем серии → .build-idea/open-questions-mode-per-series/artifacts/f2-repair.md
- [x] Owner-гейт по образцу set_chat → .build-idea/open-questions-mode-per-series/artifacts/f2-review-1.md
- [x] Видимое подтверждение владельцу → .build-idea/open-questions-mode-per-series/artifacts/f2-repair.md
- [x] Команда «в каком режиме эта серия?» → .build-idea/open-questions-mode-per-series/artifacts/f2-impl.md
- [x] Строка в чат серии о смене формата → .build-idea/open-questions-mode-per-series/artifacts/f2-traces.md
- [x] Тесты фазы написаны и зелёные (81) → .build-idea/open-questions-mode-per-series/artifacts/gate-f2.txt
- [x] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/gate-f2.txt
- [x] Четыре следа фазы → .build-idea/open-questions-mode-per-series/artifacts/f2-traces.md
- [x] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f2-review-1.md
- [x] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f2-mutation.md
- [x] Коммит фазы поимённо → d1c6a78

## Фаза 3. Поведение справочного режима

- [x] Дайджест last-only: источник — задачи, рождённые на последней прошедшей встрече → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Мелочь входит, группировка по ответственным сохранена, формат доставки прежний → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Убран запрос статусов: инструкция, нумерация, регистрация снапшота → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Мягкая подпись-шапка → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Гейт хвоста в ОБОИХ call-site (finalize + clarify_worker) → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Проверено, что пропуск блока не ломает commit_pending_shown / record_open_tasks_snapshot → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Ночной сверщик и напоминания пропускают last-only → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Reply со статусом в шпаргалке: ответ-объяснение, в стор ничего не пишется → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] При возврате в carry первый дайджест снова регистрирует снапшот → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Тесты фазы написаны и зелёные → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/f3-repair.md
- [x] Четыре следа фазы → .build-idea/open-questions-mode-per-series/artifacts/f3-traces.md
- [x] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f3-review-1.md
- [x] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f3-mutation.md
- [x] Коммит фазы поимённо → 67b1060

## Фаза 4. ISS-53 + ISS-55

- [x] Приёмочные тесты зоны написаны ПЕРВЫМИ (сторожа не было — находка Н2) → .build-idea/open-questions-mode-per-series/artifacts/f4-impl.md
- [x] reject пишет отказ по конкретному инстансу в реестр → .build-idea/open-questions-mode-per-series/artifacts/f4-diff.txt
- [x] Реестр сохраняется и при rejected > 0 → .build-idea/open-questions-mode-per-series/artifacts/f4-impl.md
- [x] Различие «нет» vs «не предлагай серию» сохранено → .build-idea/open-questions-mode-per-series/artifacts/f4-repair.md
- [x] Ретеншн отказных записей старше 30 дней → .build-idea/open-questions-mode-per-series/artifacts/f4-repair.md
- [x] fmt_when_short читает MEETING_NOTARY_DISPLAY_TZ → .build-idea/open-questions-mode-per-series/artifacts/f4-repair.md
- [x] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/f4-repair.md
- [x] Четыре следа фазы → .build-idea/open-questions-mode-per-series/artifacts/f4-traces.md
- [x] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f4-review-1.md
- [x] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f4-mutation.md
- [x] Коммит фазы поимённо → 2b8135e

## Фаза 5. ISS-54

- [x] Приёмочные тесты зоны написаны ПЕРВЫМИ (сторожа не было — находка Н2) → .build-idea/open-questions-mode-per-series/artifacts/f5-impl.md
- [x] iCalUID протянут от календарного слоя до записи реестра (находка Н1) → .build-idea/open-questions-mode-per-series/artifacts/f5-impl.md
- [x] Дедуп при записи в реестр по iCalUID, пара «время старта + URL» страховкой → .build-idea/open-questions-mode-per-series/artifacts/f5-repair.md
- [x] Дедуп в scheduler по (url, start) + лог о схлопывании → .build-idea/open-questions-mode-per-series/artifacts/f5-repair.md
- [x] SKIP/MANUAL: боевые данные вне рабочего дерева, правка запрещена условиями прогона — в файле долгов
- [x] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/f5-repair.md
- [x] Четыре следа фазы → .build-idea/open-questions-mode-per-series/artifacts/f5-traces.md
- [x] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f5-review-1.md
- [x] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f5-mutation.md
- [x] Коммит фазы поимённо → 1836d3b

## Фаза 6. ISS-51

- [ ] Детект «пришли артефакт» переведён на ИИ-первый разбор по образцу ack_understand
- [ ] Сохранён гейт detect_status_intent и позиция роута
- [ ] Регулярка осталась быстрым путём и офлайн-фолбэком, гейт дефолт-OFF в коде
- [ ] Тест на дыру Н7: глагол отправки + объект + статус-маркер не перехватывается
- [ ] Тесты фазы написаны и зелёные
- [ ] Гейт зоны зелёный после фазы → .build-idea/open-questions-mode-per-series/artifacts/gate-f6.txt
- [ ] Четыре следа фазы → .build-idea/open-questions-mode-per-series/artifacts/f6-traces.md
- [ ] Три независимых ревью по дифу → .build-idea/open-questions-mode-per-series/artifacts/f6-reviews.md
- [ ] Мутация внесена не автором и покрасила гейт → .build-idea/open-questions-mode-per-series/artifacts/f6-mutation.md
- [ ] Коммит фазы поимённо

## Фаза 7. Прод-выкатка, раскладка режимов и живой документ

- [ ] Деплой
- [ ] Раскладка режимов по каждой серии боевого реестра
- [ ] Наблюдение на живых встречах
- [ ] Живой документ обновлён
- [ ] Статусы FIXED в журнале косяков

## Этап 7. Финальная сквозная проверка

- [ ] Швы между фазами свежими агентами → .build-idea/open-questions-mode-per-series/artifacts/final-seams.md
- [ ] Полный гейт зоны зелёный → .build-idea/open-questions-mode-per-series/artifacts/gate-final.txt
- [ ] Закрывающая таблица по 10 утверждениям с командами-доказательствами → .build-idea/open-questions-mode-per-series/report.md

## Этап 8. Отчёт и сдача

- [ ] Отчёт «как было / как стало» → .build-idea/open-questions-mode-per-series/report.md
- [ ] Лист решений владельцу → .build-idea/open-questions-mode-per-series/decisions.md
- [x] Файл долгов → plans/follow-ups-open-questions-mode-per-series.md
- [x] «Что нового» для сотрудников → whats-new/2026-08-05-open-questions-mode-per-series.md
- [x] Хендофф → .build-idea/open-questions-mode-per-series/handoff.md
