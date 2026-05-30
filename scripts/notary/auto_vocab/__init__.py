"""Авто-пополнение Speechmatics vocab (Фаза 8 плана доработок 2026-05-29).

Пакет реализует гибрид из плана:
  - `sources.py`     — Слой 1: тихая авто-синхронизация из `~/Projects/me/*.md`
                       (имена/бренды/проекты), раз в день по systemd-таймеру.
  - `llm_proposer.py`— Слой 2: LLM-кандидаты после каждой встречи (best-effort,
                       не блокирует pipeline finalize).
  - `state.py`       — Слой 4: `auto_vocab_state.json` под flock (rejected,
                       approved_patterns, weekly_stats для дайджеста).
  - `applier.py`     — Слой 3: confidence-tiers (high→auto-add, low→TG). [Шаг 8.3]
  - `proposer_prompt.txt` — системный промт Claude (версионируется отдельно).

Контракт с Ф2 (тот же файл `config/speechmatics-vocab.json`):
  - дедуп по полю `content` (case-insensitive);
  - НЕ перезатирать служебные ключи `_comment`/`_phase`/`_phase2_note`;
  - НЕ трогать ручные добавки Ф2 (1С, оркестратор, Dealer 360, ...).

Архитектурное решение по LLM (отступление от буквы плана, см. handoff Ф8):
  проект ходит в Claude через `lib/claude_cli.py` (`claude --print`, подписка
  владельца), НЕ через Anthropic SDK с ANTHROPIC_API_KEY — устоявшийся паттерн
  Ф4/Ф5 (см. requirements.txt). Расходы берём из `claude --print
  --output-format json` (`total_cost_usd`), не из SDK `usage`.
"""
