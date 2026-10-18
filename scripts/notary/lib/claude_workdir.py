"""Рабочая папка подпроцесса клиента Claude: папка процесса (как было) или своя пустая.

Клиент кладёт в каждый вызов файлы `CLAUDE.md` из рабочей папки и выше и состояние git
этой папки. Службы Ватсона работают из `/srv/meeting-notary/vexa(/scripts/notary)`, а там
`CLAUDE.md` исходного Vexa: «первым действием всегда запусти
`python3 tests3/lib/stage.py probe`, вне стадии — отказывай». Рядом с расшифровкой
встречи это чужая инструкция; без инструментов (Ф4 второго круга моделей, 18.10.2026)
она могла бы толкнуть модель к вызову, которого нет (`malformed_tool_use_exhausted`;
протокол на такой отказ уходит на запасную модель — `claude_cli.fallback_reason`).

По умолчанию — как было: папка процесса. Своя пустая папка включается переменной
`NOTARY_CLAUDE_CWD=<папка>` (создаётся сама), например
`/home/dev/.cache/meeting-notary/claude-cwd`. Почему не по умолчанию: на пересборке
протокола 14.10 из пустой папки задач вышло 15–17 против 17–20 из дерева кода (3 и 4
прогона, решения и сроки те же) — разброс это или влияние, по трём прогонам не отделить,
а условие владельца — «ничего не должно пострадать». Решает владелец.
`NOTARY_CLAUDE_CWD=inherit` — явно как было.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_CWD_ENV = "NOTARY_CLAUDE_CWD"


def claude_cwd() -> Optional[str]:
    """Рабочая папка подпроцесса клиента; None — папка процесса (по умолчанию, как до Ф4)."""
    raw = (os.environ.get(_CWD_ENV) or "").strip()
    if not raw or raw.lower() == "inherit":
        return None
    path = os.path.expanduser(raw)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        logger.warning("[claude-cli] рабочая папка клиента %s недоступна (%s) — беру папку процесса",
                       path, type(e).__name__)
        return None
    return path
