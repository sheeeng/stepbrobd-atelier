import stat
import subprocess
from pathlib import Path

_HOOK = Path(__file__).parent.parent / ".github" / "actions" / "atelier" / "hook.sh"


def _hook(spool: Path, out_paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_HOOK)],
        env={"ATELIER_SPOOL": str(spool), "OUT_PATHS": out_paths},
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_appends_one_path_per_line(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    proc = _hook(spool, "/nix/store/aaa-x /nix/store/bbb-y")
    assert proc.returncode == 0
    assert spool.read_text() == "/nix/store/aaa-x\n/nix/store/bbb-y\n"


def test_hook_appends_across_invocations(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    _hook(spool, "/nix/store/aaa-x")
    _hook(spool, "/nix/store/bbb-y")
    assert spool.read_text() == "/nix/store/aaa-x\n/nix/store/bbb-y\n"


def test_hook_never_fails(tmp_path: Path) -> None:
    # an unwritable spool must not abort the build loop
    proc = _hook(tmp_path / "no" / "such" / "dir" / "spool", "/nix/store/aaa-x")
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_hook_is_executable() -> None:
    assert _HOOK.stat().st_mode & stat.S_IXUSR
