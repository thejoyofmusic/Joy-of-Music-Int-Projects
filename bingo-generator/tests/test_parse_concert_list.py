from conftest import CONCERT_DOCX

from carnatic_bingo_generator import CONCERT_TITLE_SKIP, EXPECTED_CLUES, parse_concert_clues


def test_parse_concert_clues_count_and_title(bingo_root):
    clues = parse_concert_clues(str(CONCERT_DOCX))
    assert len(clues) == 100
    assert clues[0] == "Shyama Shastri composition"
    assert clues[-1] == "Raga Navroj"
    assert "Bingo Questions" not in clues
    assert CONCERT_TITLE_SKIP not in clues
    assert all(c.strip() for c in clues)


def test_parser_supports_manual_numbering_and_rejects_short_pool(tmp_path):
    import pytest
    from docx import Document

    path = tmp_path / "questions.docx"
    document = Document()
    document.add_heading("New concert clues", level=1)
    for index in range(30):
        document.add_paragraph(f"{index + 1}. Concert clue {index}")
    document.save(path)
    clues = parse_concert_clues(str(path))
    assert len(clues) == 30
    assert clues[0] == "Concert clue 0"

    short = Document()
    short.add_paragraph("Bingo Questions")
    short.add_paragraph("1. Only one clue")
    short.save(path)
    with pytest.raises(ValueError, match="at least 24"):
        parse_concert_clues(str(path))
