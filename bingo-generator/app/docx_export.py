"""Render a stored bingo sheet as the printable .docx from the reference template.

The Word layout (fonts, logos, 6x6 grid, "I Heard" table, MCQ page) is owned by
``input_reference_bingo_sheet.docx``. ``carnatic_bingo_generator.build_bingo_docx``
fills that template, so the download reuses it instead of duplicating the layout
logic. The only difference is the destination: the web app writes to memory and
streams the bytes back, while the CLI writes a file.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Sequence

from app.models import BingoSheet


def render_sheet_docx(
    sheet: BingoSheet,
    clue_texts: Sequence[str],
    mcq: dict[str, Any],
) -> bytes:
    """Return the printable .docx for ``sheet`` as bytes.

    ``clue_texts`` must be the 24 clues in board order (A1..E5, skipping C3) and
    ``mcq`` must provide the keys ``question``, ``options`` and ``answer``.
    """
    from carnatic_bingo_generator import build_bingo_docx, get_template_path

    buffer = BytesIO()
    build_bingo_docx(
        sheet_number=sheet.id,
        clues=list(clue_texts),
        clue_order=list(range(len(clue_texts))),
        mcq=mcq,
        output_path=buffer,
        template_path=get_template_path(),
    )
    return buffer.getvalue()
