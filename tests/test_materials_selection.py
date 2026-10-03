import io
import zipfile
from types import SimpleNamespace as NS

import pytest

from bot.services import materials as M
from bot.services.selection import parse_selection


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def test_zip_filters_and_guards():
    r = M.read_zip(make_zip({"lab1.py": b"print(1)", "../evil.txt": b"x", "notes.md": b"hi", "img.png": b"x",
                             "__MACOSX/a.txt": b"x", "bomb.txt": b"0" * 5_000_000}))
    assert [n for n, _ in r.files] == ["lab1.py", "notes.md"]
    assert len(r.skipped) == 4 and any("compression" in s for s in r.skipped)
    with pytest.raises(ValueError):
        M.read_zip(b"not a zip")


def test_extract_text_kinds():
    assert M.extract_text("a.py", b"x=1").text == "x=1"
    with pytest.raises(ValueError):
        M.extract_text("a.exe", b"x")
    big = M.extract_text("a.txt", b"a" * (M.MAX_TEXT_PER_FILE + 10))
    assert len(big.text) == M.MAX_TEXT_PER_FILE and big.warning


def test_retrieve_ranks_relevant_chunk():
    src = [("slides.txt", "Binary search trees insert delete rotate AVL balance factor. " * 30 +
            "\n\n" + "Unrelated notes about shell scripting and grep awk sed. " * 30)]
    out = M.retrieve(src, "AVL tree rotations", k=1)
    assert out and "AVL" in out[0] and "[slides.txt]" in out[0]
    assert M.retrieve([], "x") == []


ITEMS = [NS(type=t, seq=i) for t in ("lab", "tutorial", "assignment") for i in range(1, 7)]


def test_selection():
    sel, p = parse_selection("labs 2-5 and assignment 1", ITEMS)
    assert [(i.type, i.seq) for i in sel] == [("lab", 2), ("lab", 3), ("lab", 4), ("lab", 5), ("assignment", 1)] and not p
    assert len(parse_selection("all tutorials", ITEMS)[0]) == 6
    assert len(parse_selection("everything", ITEMS)[0]) == 18
    sel, p = parse_selection("lab 9", ITEMS)
    assert not sel and "no lab 9" in p[0]
    assert parse_selection("blah", ITEMS)[1]
