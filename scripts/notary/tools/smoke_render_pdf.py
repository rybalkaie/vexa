#!/usr/bin/env python3
"""Деплой-смоук PDF-рендера протоколов (RISK4 плана protocol-pdf-telegram).

Рендерит эталонный `.md` (кириллица + цветные эмодзи + таблица) в PDF и проверяет:
  1. PDF собран, начинается с `%PDF`, размер > 10 КБ (REQ 1.2).
  2. В PDF есть растровый image-XObject — прокси «цветные эмодзи срендерились».
     Chromium растеризует цветные эмодзи в картинки; если `fonts-noto-color-emoji`
     отсутствует/деградировал после `apt upgrade`, эмодзи станут ч/б векторными
     (из текстового шрифта) → image-XObject не будет → смоук падает и ловит
     деградацию ДО боевой встречи. `PROTOCOL_PDF_SMOKE_SKIP_EMOJI=1` понижает
     эту проверку до warning (если на конкретном chromium прокси ненадёжен).

Запуск (на VPS, в деплой-флоу, ПОСЛЕ установки chromium + шрифтов):
    PROTOCOL_PDF_CHROME_BIN=chromium python3 tools/smoke_render_pdf.py

Exit 0 — рендер здоров; 1 — деградация / сбой (НЕ переключать боевую доставку).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS.parent.parent))

from notary.lib import protocol_to_pdf as ppdf  # noqa: E402

FIXTURE = THIS.parent / "tests" / "fixtures" / "protocol_pdf_sample.md"


def _has_image_xobject(data: bytes) -> bool:
    return (
        b"/Image" in data
        or b"/Subtype/Image" in data
        or b"/Subtype /Image" in data
    )


def main() -> int:
    if not FIXTURE.is_file():
        print(f"❌ нет эталонной фикстуры: {FIXTURE}")
        return 1
    md = FIXTURE.read_text(encoding="utf-8")

    try:
        chrome = ppdf.find_chrome_binary()
    except ppdf.PdfRenderError as e:
        print(f"❌ браузер для PDF не найден: {e}")
        return 1
    print(f"chrome: {chrome}")

    with tempfile.TemporaryDirectory(prefix="smoke-render-pdf-") as td:
        out = Path(td) / "smoke.pdf"
        try:
            ppdf.render_pdf_from_markdown(
                md, str(out),
                title="Смоук PDF — 03.06.2026",
                subtitle="Участники: Илья Рыбалка, Михаил Еремеев  ·  Чистое время обсуждения: ~22 мин",
            )
        except ppdf.PdfRenderError as e:
            print(f"❌ рендер упал: {e}")
            return 1

        data = out.read_bytes()
        size = len(data)
        ok_pdf = data[:4] == b"%PDF"
        ok_size = size > 10 * 1024
        has_image = _has_image_xobject(data)
        print(
            f"size={size} bytes (>10KB: {ok_size}) | %PDF: {ok_pdf} | "
            f"color-emoji-proxy(image-xobject): {has_image}"
        )

        if not (ok_pdf and ok_size):
            print("❌ PDF битый или подозрительно мал")
            return 1
        if not has_image:
            skip = (os.environ.get("PROTOCOL_PDF_SMOKE_SKIP_EMOJI") or "").strip().lower()
            if skip in ("1", "true", "yes"):
                print("⚠️ нет image-XObject (цветные эмодзи?) — пропущено (SKIP_EMOJI=1)")
            else:
                print(
                    "❌ нет image-XObject: цветные эмодзи не срендерились — поставь "
                    "fonts-noto-color-emoji на VPS (см. README «PDF протоколов»)"
                )
                return 1
        print("✅ PASS — PDF-рендер здоров")
        return 0


if __name__ == "__main__":
    sys.exit(main())
