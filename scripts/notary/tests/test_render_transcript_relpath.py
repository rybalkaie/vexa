"""Регресс: render_protocol должен принимать transcript_relpath и класть его в шапку.

Баг (коммит 3e40731): finalize-meeting.py звал render_protocol(transcript_relpath=...),
а сигнатура lib/render.py этого аргумента не имела → TypeError на КАЖДОЙ
speechmatics-встрече, падение на Step 5/5 render. 462 теста это пропустили, т.к.
ни один не дёргал реальный вызов render с этим kwargs. Вскрыто на восстановлении 01.06.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib.render import render_protocol  # noqa: E402


class TestRenderTranscriptRelpath(unittest.TestCase):
    def _render(self, **kwargs) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("# {{ meeting_title }}\n**Транскрипт:** {{ transcript_relpath }}\n\n{{ transcript_body }}\n")
            tpl = fh.name
        return render_protocol(
            template_path=tpl,
            turns=[],
            meta={"meeting_title": "Координация", "startTs": "2026-06-01T09:00:00Z"},
            sources_used=["regex_pymorphy3"],
            asr_model="speechmatics-enhanced",
            **kwargs,
        )

    def test_accepts_transcript_relpath_and_puts_in_header(self):
        # Раньше падало TypeError ещё до рендера.
        out = self._render(transcript_relpath="_transcripts/2026-06-01.txt")
        self.assertIn("_transcripts/2026-06-01.txt", out)

    def test_transcript_relpath_optional(self):
        # Без аргумента — не падает, плейсхолдер заполняется прочерком.
        out = self._render()
        self.assertIn("**Транскрипт:** —", out)


if __name__ == "__main__":
    unittest.main()
