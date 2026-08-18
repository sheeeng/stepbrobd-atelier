import ast
import sys
from pathlib import Path

from atelier import stream
from atelier.stream import read_new


def test_read_new_returns_complete_lines_and_offset(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n/nix/store/bbb-y\n")
    lines, offset = read_new(spool, 0)
    assert lines == ["/nix/store/aaa-x", "/nix/store/bbb-y"]
    assert offset == len(spool.read_bytes())


def test_read_new_resumes_from_offset(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n")
    _, offset = read_new(spool, 0)
    spool.write_text("/nix/store/aaa-x\n/nix/store/bbb-y\n")
    lines, _ = read_new(spool, offset)
    assert lines == ["/nix/store/bbb-y"]


def test_read_new_holds_back_partial_trailing_line(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n/nix/store/bb")
    lines, offset = read_new(spool, 0)
    assert lines == ["/nix/store/aaa-x"]
    spool.write_text("/nix/store/aaa-x\n/nix/store/bbb-y\n")
    lines, _ = read_new(spool, offset)
    assert lines == ["/nix/store/bbb-y"]


def test_read_new_skips_blank_lines(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("\n/nix/store/aaa-x\n\n")
    lines, _ = read_new(spool, 0)
    assert lines == ["/nix/store/aaa-x"]


def test_read_new_missing_spool_reads_as_empty(tmp_path: Path) -> None:
    assert read_new(tmp_path / "spool", 0) == ([], 0)


def test_read_new_idle_returns_same_offset(tmp_path: Path) -> None:
    # the poll loop's hottest path, an unchanged spool must not re-yield
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n")
    _, offset = read_new(spool, 0)
    assert read_new(spool, offset) == ([], offset)


def test_read_new_no_newline_yet(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aa")
    assert read_new(spool, 0) == ([], 0)


def test_read_new_consumes_blank_lines(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_text("\n/nix/store/aaa-x\n\n")
    _, offset = read_new(spool, 0)
    assert offset == len(spool.read_bytes())


def test_module_keeps_python_312_floor() -> None:
    # build cells run this file with the runner's system python3, 3.12 on ubuntu
    src = Path(stream.__file__).read_text()
    ast.parse(src, feature_version=(3, 12))
    tree = ast.parse(src)
    imported = {
        n.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for n in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= sys.stdlib_module_names
