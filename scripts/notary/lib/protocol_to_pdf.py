"""MD протокол → красивый HTML → PDF через headless Chromium (Linux/VPS).

Порт `~/.local/bin/protocol-to-pdf` (мак, Google Chrome) на VPS, где Chrome нет.
Отличия от мак-образца (Ф2 плана `protocol-pdf-telegram`):
  - Бинарь браузера резолвится через env `PROTOCOL_PDF_CHROME_BIN`, с
    автодетектом (`chromium`, `chromium-browser`, `google-chrome`,
    `google-chrome-stable`, mac-путь Google Chrome). REQ 1.2.
  - Флаг `--no-sandbox` (RISK4): под не-root юзером в контейнере/VPS Chrome без
    него не стартует.
  - Изолированный `--user-data-dir` на каждый рендер — иначе при параллельных
    finalize два headless-Chrome дерутся за дефолтный профиль («profile in use»).
  - `markdown` импортируется ЛЕНИВО внутри рендера: модуль обязан импортироваться
    из `llm_postprocess` (его тянет listener под venv-cli), даже если пакет
    `markdown` ещё не установлен — тогда падает только сам рендер (→ алерт Илье,
    REQ 1.4), а не импорт listener'а.

Рендер идентичен мак-образцу: синяя шапка `#2c5282`, H2 с подчёркиванием,
таблицы с цветным заголовком и zebra, ⚠️-врезки `blockquote`, цветные эмодзи и
кириллица (их даёт сам Chromium + системные шрифты — на VPS нужен
`fonts-noto-color-emoji`, см. README раздел «PDF протоколов»).

Системные зависимости (НЕ ставит этот модуль — деплой владельца, см. README):
  - chromium-headless (бинарь браузера);
  - fonts-noto-color-emoji (цветные эмодзи; без них эмодзи ч/б);
  - python-пакет `markdown` в том же venv, что finalize/listener.
"""
from __future__ import annotations

import html as htmllib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PdfRenderError(RuntimeError):
    """Сбой сборки PDF (нет браузера / нет `markdown` / Chrome упал / пустой PDF)."""


# Таймаут сборки PDF — как в мак-образце (RISK4: chromium может зависнуть).
DEFAULT_TIMEOUT_SEC = 90

# Минимальный валидный размер PDF (REQ 1.2: «валидный PDF >10 КБ»). Меньше —
# почти наверняка пустой/битый рендер (Chrome молча отдал заглушку).
MIN_VALID_PDF_BYTES = 10 * 1024

# Кандидаты бинаря браузера для автодетекта (порядок = приоритет). Env
# `PROTOCOL_PDF_CHROME_BIN` перебивает всё. На VPS целевой — `chromium`.
_CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)

# mac-путь Google Chrome (для локального теста/смоука; на VPS его нет).
_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# CSS — один в один с мак-образцом `~/.local/bin/protocol-to-pdf` (план: «тот же
# CSS/HTML»). Менять только синхронно с образцом.
CSS = """
@page { size: A4; margin: 17mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Helvetica Neue", "Segoe UI", Arial, sans-serif;
       font-size: 11pt; line-height: 1.55; color: #1f2430; margin: 0; }
h1 { font-size: 19pt; color: #1a2b4a; margin: 0 0 4px; line-height: 1.25; }
.subtitle { color: #5b6472; font-size: 10pt; margin: 0 0 2px; }
.hdr { border-bottom: 3px solid #2c5282; padding-bottom: 12px; margin-bottom: 18px; }
h2 { font-size: 13pt; color: #2c5282; margin: 20px 0 7px; padding: 0 0 4px;
     border-bottom: 1px solid #e2e8f0; break-after: avoid; }
h3 { font-size: 11.5pt; color: #2d3748; margin: 14px 0 5px; }
p { margin: 6px 0; }
ul { margin: 6px 0; padding-left: 20px; }
/* Ф3 (open-questions-reply-fix): нумерованные висяки (дайджест/протокол) — `<ol>` с
   отступом под номер. ISS-44.5: маркер («13.») рисуется в левом жёлобе (list-style-position
   outside); 20px хватало на однозначное «1.», но двузначные «10.»–«18.» шире — первая цифра
   уезжала за левый край и обрезалась. 34px вмещает двузначный маркер. Не касается `ul`
   (там точка, ширина от числа не зависит). */
ol { margin: 6px 0; padding-left: 34px; }
li { margin: 4px 0; }
li::marker { color: #94a3b8; }
strong { color: #111827; font-weight: 650; }
hr { border: none; border-top: 1px solid #e8ecf1; margin: 16px 0; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt;
        break-inside: avoid; }
thead th { background: #2c5282; color: #fff; text-align: left; padding: 7px 10px; font-weight: 600; }
td { border: 1px solid #d6dde6; padding: 6px 10px; vertical-align: top; }
tbody tr:nth-child(even) td { background: #f6f9fc; }
blockquote { background: #fff8ef; border-left: 4px solid #ed8936; padding: 8px 13px;
             margin: 11px 0; color: #7a4a16; font-size: 10pt; border-radius: 0 4px 4px 0; }
blockquote p { margin: 3px 0; }
code { background: #eef1f5; padding: 1px 5px; border-radius: 3px; font-size: 9.5pt; }
em { color: #5b6472; }
a { color: #2563eb; text-decoration: none; }
/* Ф4 (ISS-34, A5): статус висяка словом+цветом. Оттенки в семье оранжевого
   blockquote (#ed8936): .status-open — тёмно-оранжевый «уточнить», .status-done —
   зелёный жирный «Закрыто». Третий цвет НЕ заводим (WAIT/CANCELLED/REOPEN без цвета). */
.status-open { color: #c05621; font-weight: 600; }
.status-done { color: #2f855a; font-weight: 700; }
.footer { margin-top: 22px; padding-top: 8px; border-top: 1px solid #e8ecf1;
          color: #9aa3b0; font-size: 8.5pt; }
"""


def find_chrome_binary() -> str:
    """Возвращает путь к бинарю браузера или бросает `PdfRenderError`.

    Приоритет: env `PROTOCOL_PDF_CHROME_BIN` → `chromium`/`chromium-browser`/
    `google-chrome`/`google-chrome-stable` на PATH → mac-путь Google Chrome.
    """
    env_bin = (os.environ.get("PROTOCOL_PDF_CHROME_BIN") or "").strip()
    if env_bin:
        # Явный путь или имя на PATH.
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        resolved = shutil.which(env_bin)
        if resolved:
            return resolved
        raise PdfRenderError(
            f"PROTOCOL_PDF_CHROME_BIN={env_bin!r} не найден / не исполняемый"
        )
    for name in _CHROME_CANDIDATES:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    if os.path.isfile(_MAC_CHROME) and os.access(_MAC_CHROME, os.X_OK):
        return _MAC_CHROME
    raise PdfRenderError(
        "браузер для PDF не найден: задай PROTOCOL_PDF_CHROME_BIN или поставь "
        "chromium (см. README «PDF протоколов»)"
    )


# A2.2: поля шапки протокола, которые УЖЕ напечатаны в визуальной шапке PDF
# (title = «Серия — дата»; subtitle = «Участники: … · Чистое время: …»). Их
# строки из тела убираем, иначе участники/длительность печатаются ДВАЖДЫ (раз
# в subtitle, раз в `**Участники:**` тела — баг владельца «два раза участники
# написаны»). `**Транскрипт:**` — относительная ссылка, в PDF бесполезна, тоже
# убираем. `**Встреча:**` (тема) ОСТАВЛЯЕМ — её в визуальной шапке нет.
_PDF_DROP_HEADER_FIELD_RE = re.compile(
    r"^\s*\*\*\s*(участ\w*|длител\w*|транскрипт\w*)\s*:\*\*",
    re.IGNORECASE,
)


def _strip_leading_heading(md_text: str) -> str:
    """Готовит тело протокола к PDF: отрезает технический H1-маркер
    (`#протоколвстречи …`) и ДУБЛИРУЮЩИЕ поля шапки (участники/длительность/
    транскрипт), которые уже отрисованы в визуальной шапке PDF (title+subtitle).
    Иначе участники печатаются дважды (A2.2). Тему `**Встреча:**` оставляем."""
    lines = (md_text or "").splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    lines = [ln for ln in lines if not _PDF_DROP_HEADER_FIELD_RE.match(ln)]
    return "\n".join(lines).strip()


# FU-4 (security): тело протокола генерится из транскрипта (НЕДОВЕРЕННЫЙ вход).
# Allowlist для bleach — только структурные теги, которые реально даёт markdown
# (tables/sane_lists/fenced_code). Всё прочее (<script>, <img>, on*-атрибуты,
# <iframe>, <style>) вырезается, иначе headless-Chrome (--no-sandbox, file://)
# их исполнит/подгрузит → egress-маячок / чтение локальных файлов.
_PDF_ALLOWED_TAGS = [
    "p", "br", "hr", "span", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "ul", "ol", "li", "blockquote",
    "code", "pre", "a",
    "table", "thead", "tbody", "tr", "td", "th",
]
# Ф4 (ISS-34): `span[class]` разрешён для наших статус-span'ов (status-open/status-done),
# которые вешает `_colorize_status_labels` ДО bleach. ТОЛЬКО `class` (не `style`) —
# class безвреден (не исполняется, не грузит ресурсы), а `style`/`on*` открыли бы дыру
# egress/чтения файлов (см. security-комментарий выше). Из недоверенного тела markdown
# сам `<span>` не порождает, так что реальные span'ы — только наши.
_PDF_ALLOWED_ATTRS = {"span": ["class"], "a": ["href", "title"], "ol": ["start"]}
# Ф3 (open-questions-reply-fix): `ol[start]` — чтобы СКВОЗНАЯ нумерация висяков не
# рестартилась. Каждая секция дайджеста/протокола (безымянные, затем по людям) —
# отдельный `<ol>`; markdown ставит `start="N"` (2-я секция начинается не с 1). Без
# `start` в allowlist bleach его срезал бы → браузер отрисовал бы каждую секцию с «1.»
# (показ разошёлся бы со слепком нумерации). `start` — числовой атрибут показа, без
# XSS/egress-вектора (в отличие от `style`/`on*`), пускать безопасно.


# Ф4 (ISS-34, A5): канонические статус-ярлыки висяка → цветной span. Красим ТОЛЬКО
# точную каноническую строку (риск ISS-34: regex по свободному тексту хрупок → ярлык
# сперва канонизирован в промпте протокола / буллетах дайджеста, потом красим по нему).
# Отклонился ярлык от каноники → пункт без цвета (мягкая деградация, не поломка).
# Строки-ярлыки — зеркало `series_memory.STATUS_OPEN_LABEL`/`STATUS_DONE_LABEL` (единый
# источник там; здесь не импортируем ради лёгкости PDF-модуля, консистентность держит
# тест `test_iss34_phase4_display`).
#
# Оранжевый «Статус неизвестен — уточнить» — матч по всей фразе (уникальна); внутренний
# em-dash терпим по ширине пробелов (модель/рендер дрейфуют).
_STATUS_OPEN_RE = re.compile(r"Статус\s+неизвестен\s*—\s*уточнить")
_STATUS_OPEN_SPAN = '<span class="status-open">Статус неизвестен — уточнить</span>'
# Зелёное жирное «Закрыто» — ТОЛЬКО как статус после разделителя «— » (так его пишет
# модель: «- <задача> — Закрыто»). Lookbehind на «— » исключает заголовок Части 2
# «✅ Закрыто с прошлых встреч» (там «Закрыто» после «✅ ») и слово «закрыто» в тексте
# задачи (строчное / без разделителя). Правую границу проверяем lookahead'ом.
_STATUS_DONE_RE = re.compile(r"(—\s*)Закрыто(?=[\s<.,;:!?)]|$)")
_STATUS_DONE_SPAN = r'\1<span class="status-done">Закрыто</span>'


def _colorize_status_labels(html: str) -> str:
    """Ф4 (ISS-34, A5): оборачивает канонические статус-ярлыки висяка в цветные span'ы.

    Вызывается ПОСЛЕ `markdown.markdown` и ДО `bleach.clean` (bleach пропускает
    span[class], см. `_PDF_ALLOWED_ATTRS`). Идёт по HTML — статус лежит внутри
    `<li>`/`<p>`. Оранжевый прогоняем ПЕРВЫМ (в нём свой «—», но нет «Закрыто» — зелёный
    его не заденет). НЕ трогает WAIT/CANCELLED/REOPEN и заголовки подразделов (НЕС2):
    красит строго две канонические строки — прочий статус остаётся без цвета."""
    html = _STATUS_OPEN_RE.sub(_STATUS_OPEN_SPAN, html)
    html = _STATUS_DONE_RE.sub(_STATUS_DONE_SPAN, html)
    return html


def markdown_to_html(md_text: str, title: str, subtitle: str) -> str:
    """MD → полный HTML-документ с фирменным CSS (как мак-образец).

    `markdown` импортируется ЛЕНИВО — отсутствие пакета даёт `PdfRenderError`,
    а не ImportError на импорте модуля (см. docstring модуля).
    Тело протокола санитизируется `bleach` (FU-4) — недоверенный вход.
    """
    try:
        import markdown  # noqa: PLC0415
    except ImportError as e:
        raise PdfRenderError(
            "python-пакет `markdown` не установлен в этом venv "
            "(pip install markdown — см. README «PDF протоколов»)"
        ) from e
    body_md = _strip_leading_heading(md_text)
    body_html = markdown.markdown(
        body_md, extensions=["tables", "sane_lists", "fenced_code"]
    )
    # Ф4 (ISS-34): красим канонические статус-ярлыки висяка ДО bleach (он пропускает
    # наши span[class]). После markdown, чтобы работать по готовому HTML тела.
    body_html = _colorize_status_labels(body_html)
    # FU-4 (security): вырезаем сырой HTML из тела (см. _PDF_ALLOWED_TAGS выше).
    # title/subtitle экранируются htmllib.escape отдельно (это не markdown).
    try:
        import bleach  # noqa: PLC0415
    except ImportError as e:
        raise PdfRenderError(
            "python-пакет `bleach` не установлен в этом venv "
            "(pip install bleach — санитизация HTML протокола, FU-4)"
        ) from e
    body_html = bleach.clean(
        body_html,
        tags=_PDF_ALLOWED_TAGS,
        attributes=_PDF_ALLOWED_ATTRS,
        strip=True,
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">\n'
        f"<style>{CSS}</style></head><body>\n"
        f'<div class="hdr"><h1>{htmllib.escape(title)}</h1>\n'
        f'<div class="subtitle">{htmllib.escape(subtitle)}</div></div>\n'
        f"{body_html}\n"
        '<div class="footer">Протокол подготовлен ботом-нотариусом · meeting-notary</div>\n'
        "</body></html>"
    )


def _want_isolated_profile(chrome_bin: str) -> bool:
    """Нужен ли отдельный `--user-data-dir` на этот рендер.

    Да — на Linux/VPS (chromium делит дефолтный профиль → параллельные finalize
    дерутся за SingletonLock). Нет — на macOS-деве (запущенный GUI Google Chrome
    конфликтует с отдельным профилем → зависание; образцовый путь без профиля
    работает). Override: env `PROTOCOL_PDF_NO_USER_DATA_DIR=1` форсит «без профиля».
    """
    raw = (os.environ.get("PROTOCOL_PDF_NO_USER_DATA_DIR") or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return False
    if sys.platform == "darwin":
        return False
    return True


def _build_chrome_cmd(chrome: str, out_path: Path, html_path: Path,
                      profile_dir: Optional[str]) -> list:
    """Командная строка headless-браузера для печати PDF.

    `--no-sandbox` всегда (RISK4: под не-root в контейнере/VPS Chrome без него не
    стартует; на маке безвреден). `--user-data-dir` + first-run-флаги — только
    когда `profile_dir` задан (см. `_want_isolated_profile`).
    """
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
    ]
    if profile_dir is not None:
        cmd += [
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    cmd += [f"--print-to-pdf={out_path}", "file://" + str(html_path)]
    return cmd


def stored_pdf_path(md_path) -> Path:
    """Где лежит ГОТОВЫЙ PDF протокола — рядом с самим протоколом.

    🔴 ЗАЧЕМ ФАЙЛ ВООБЩЕ ЛОЖИТСЯ НА ДИСК (замечание владельца 25.08.2026).
    Портал просил PDF у шлюза в момент нажатия кнопки, шлюз запускал headless-
    браузер — и под лимитами своего юнита (192 МБ памяти, 64 задачи) браузер
    падал: человек видел «PDF временно недоступен» на каждой встрече. Собирать
    заново документ, который уже собирали при отправке в Telegram, незачем: PDF
    протокола неизменен, пока неизменен сам протокол.

    🔴 НОВЫХ ДАННЫХ НА ДИСКЕ НЕ ПОЯВЛЯЕТСЯ. Рядом уже лежит `<date>-protokol.md`
    с тем же текстом и в той же папке с теми же правами — PDF не расширяет круг
    того, что хранится, и не меняет срок хранения (его чистит тот же метельщик,
    что и протокол).

    Имя — `<date>-protokol.pdf` рядом с `<date>-protokol.md`.
    """
    return Path(md_path).with_suffix(".pdf")


def is_stored_pdf_fresh(md_path, pdf_path=None) -> bool:
    """Готовый PDF годен, только если он НЕ СТАРШЕ своего протокола.

    🔴 СРАВНЕНИЕ ПО ВРЕМЕНИ ПРАВКИ — НЕСУЩЕЕ. Протокол переписывается: человек
    присылает поправку в Telegram, портал правит текст, бот пересобирает версию.
    Отдать «какой-нибудь» лежащий PDF значило бы выдать человеку СТАРУЮ редакцию
    протокола как текущую — и он бы этого не заметил.
    """
    md = Path(md_path)
    pdf = Path(pdf_path) if pdf_path is not None else stored_pdf_path(md)
    try:
        if not pdf.is_file() or not md.is_file():
            return False
        if pdf.stat().st_size < MIN_VALID_PDF_BYTES:
            return False
        return pdf.stat().st_mtime >= md.stat().st_mtime
    except OSError:
        return False


def render_pdf_from_markdown(
    md_text: str,
    out_pdf: str,
    *,
    title: str,
    subtitle: str,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    chrome_bin: Optional[str] = None,
) -> Path:
    """Собирает PDF из markdown-протокола через headless-браузер.

    Пишет HTML рядом с PDF, гоняет `<chrome> --headless --print-to-pdf`, проверяет
    что PDF существует и не пустой. Любой сбой (нет браузера/`markdown`, Chrome
    упал/таймаут, PDF пустой) → `PdfRenderError` — caller (deliver_protocol)
    переводит его в алерт Илье (REQ 1.4), текстом протокол НЕ шлёт.

    Возвращает `Path` к собранному PDF.
    """
    chrome = chrome_bin or find_chrome_binary()
    out_path = Path(out_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_doc = markdown_to_html(md_text, title, subtitle)
    # ─────────────────────────────────────────────────────────────────────────
    # 🔴 ПРОМЕЖУТОЧНЫЙ HTML ЖИВЁТ ВО ВРЕМЕННОЙ ПАПКЕ, А НЕ РЯДОМ С PDF
    # (сверка 26.08.2026).
    #
    # ЧТО БЫЛО. Файл писался как `out_path.with_suffix(".html")`. Пока PDF
    # собирался во временный каталог, HTML умирал вместе с ним. С 26.08 PDF
    # кладётся РЯДОМ С ПРОТОКОЛОМ — и рядом же начал оседать HTML: в папке
    # протоколов появилась ТРЕТЬЯ копия того же текста, о которой не знает ни
    # метельщик, ни уборка по сроку хранения. То есть дословный разговор
    # переживал бы удаление `.md` и `.pdf`, для которых срок объявлен.
    #
    # Временный каталог снимает оба вопроса разом: мусора нет, лишней копии нет,
    # и поведение больше не зависит от того, куда просят положить PDF.
    # ─────────────────────────────────────────────────────────────────────────
    html_dir = tempfile.mkdtemp(prefix="protocol-pdf-html-")
    html_path = Path(html_dir) / "protocol.html"
    try:
        html_path.write_text(html_doc, encoding="utf-8")
    except OSError as e:
        shutil.rmtree(html_dir, ignore_errors=True)
        raise PdfRenderError(f"не записал HTML для PDF: {e}") from e

    # Изолированный профиль (`--user-data-dir`) на каждый рендер — защита от
    # «profile in use» при параллельных finalize на VPS (chromium делит дефолтный
    # профиль → SingletonLock). НО на macOS-деве уже запущенный GUI Google Chrome
    # конфликтует с отдельным профилем (рендер зависает) — там идём образцовым
    # путём без `--user-data-dir`. Управляется `_want_isolated_profile`.
    tmp_profile = None
    try:
        profile_dir = None
        if _want_isolated_profile(chrome):
            tmp_profile = tempfile.mkdtemp(prefix="protocol-pdf-chrome-")
            profile_dir = tmp_profile
        cmd = _build_chrome_cmd(chrome, out_path, html_path, profile_dir)
        try:
            proc = subprocess.run(
                cmd, check=False, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfRenderError(
                f"PDF-рендер превысил таймаут {timeout}с (chromium завис?)"
            ) from e
        except OSError as e:
            raise PdfRenderError(
                f"не смог запустить браузер {chrome!r}: {e}"
            ) from e
    finally:
        if tmp_profile:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        shutil.rmtree(html_dir, ignore_errors=True)

    if proc.returncode != 0:
        # stderr НЕ логируем целиком (может содержать пути) — только хвост кода.
        tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
        raise PdfRenderError(
            f"браузер вернул код {proc.returncode} при сборке PDF: {tail}"
        )
    if not out_path.is_file():
        raise PdfRenderError("браузер отработал, но PDF-файл не создан")
    size = out_path.stat().st_size
    if size < MIN_VALID_PDF_BYTES:
        raise PdfRenderError(
            f"PDF подозрительно мал ({size} байт < {MIN_VALID_PDF_BYTES}) — "
            "вероятно битый рендер"
        )
    logger.info("[protocol-pdf] rendered ok bytes=%d via %s", size, Path(chrome).name)
    return out_path
