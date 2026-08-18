import ast
import io
import stat
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from atelier import stream
from atelier.stream import Attic, Cachix, Niks3, backends, push, read_new, ready


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


def _record(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(
        argv: list[str], stdin: str | None = None, env: dict[str, str] | None = None
    ) -> None:
        calls.append(list(argv))

    monkeypatch.setattr(stream, "_run", fake)
    monkeypatch.setattr(stream.shutil, "which", lambda _: "/bin/true")
    return calls


def test_backends_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ATTIC_SERVER",
        "ATTIC_CACHE",
        "ATTIC_TOKEN",
        "CACHIX_CACHE",
        "NIKS3_SERVER",
        "NIKS3_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    assert backends() == []
    monkeypatch.setenv("ATTIC_SERVER", "https://a.example")
    # attic needs both server and cache
    assert backends() == []
    monkeypatch.setenv("ATTIC_CACHE", "c")
    monkeypatch.setenv("CACHIX_CACHE", "d")
    monkeypatch.setenv("NIKS3_SERVER", "https://n.example")
    assert backends() == [
        Attic("https://a.example", "c", ""),
        Cachix("d"),
        Niks3("https://n.example", ""),
    ]


def test_attic_setup_and_push(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    b = Attic("https://a.example", "c", "tok")
    b.setup()
    b.push(["/nix/store/aaa-x"])
    assert calls == [
        ["attic", "login", "default", "https://a.example", "tok"],
        ["attic", "cache", "info", "c"],
        ["attic", "push", "c", "/nix/store/aaa-x"],
    ]


def test_ensure_installs_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        stream, "_run", lambda argv, stdin=None, env=None: calls.append(list(argv))
    )
    monkeypatch.setattr(stream.shutil, "which", lambda _: None)
    monkeypatch.setenv("CACHIX_AUTH_TOKEN", "t")
    Cachix("d").setup()
    assert calls == [["nix", "profile", "install", "nixpkgs#cachix"]]


def test_cachix_push_paths_on_stdin_and_drops_empty_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake(
        argv: list[str], stdin: str | None = None, env: dict[str, str] | None = None
    ) -> None:
        seen["argv"], seen["stdin"], seen["env"] = argv, stdin, env

    monkeypatch.setattr(stream, "_run", fake)
    monkeypatch.setenv("CACHIX_SIGNING_KEY", "")
    monkeypatch.setenv("ATELIER_CANARY", "1")
    Cachix("d").push(["/nix/store/aaa-x", "/nix/store/bbb-y"])
    assert seen["argv"] == ["cachix", "push", "d"]
    assert seen["stdin"] == "/nix/store/aaa-x\n/nix/store/bbb-y\n"
    env = seen["env"]
    assert isinstance(env, dict)
    assert "CACHIX_SIGNING_KEY" not in env
    assert env.get("ATELIER_CANARY") == "1"


def test_cachix_preserves_nonempty_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake(
        argv: list[str], stdin: str | None = None, env: dict[str, str] | None = None
    ) -> None:
        seen["env"] = env

    monkeypatch.setattr(stream, "_run", fake)
    monkeypatch.setenv("CACHIX_SIGNING_KEY", "key")
    Cachix("d").push(["/nix/store/aaa-x"])
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["CACHIX_SIGNING_KEY"] == "key"


def test_niks3_token_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _record(monkeypatch)
    monkeypatch.setattr(stream.tempfile, "tempdir", str(tmp_path))
    b = Niks3("https://n.example", "tok")
    b.setup()
    assert b.auth is not None
    flag, path = b.auth
    assert flag == "--auth-token-path"
    assert Path(path).read_text() == "tok"
    # mkstemp creates the token file private to the runner user
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    b.push(["/nix/store/aaa-x"])
    assert calls[-1] == [
        "niks3",
        "push",
        "--server-url",
        "https://n.example",
        "--auth-token-path",
        path,
        "/nix/store/aaa-x",
    ]


def test_niks3_oidc_auth_writes_mint_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record(monkeypatch)
    monkeypatch.setattr(stream.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://gh.example/token")
    monkeypatch.setattr(stream, "_audience", lambda _: "https://n.example")
    b = Niks3("https://n.example", "")
    b.setup()
    assert b.auth is not None
    flag, path = b.auth
    assert flag == "--auth-token-script"
    body = Path(path).read_text()
    assert "&audience=https%3A%2F%2Fn.example" in body
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in body
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o700


def test_audience_parses_cache_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(url: str, timeout: float = 0) -> io.BytesIO:
        seen.append(url)
        return io.BytesIO(b'{"oidc_audience": "https://n.example"}')

    monkeypatch.setattr(stream.urllib.request, "urlopen", fake)
    assert stream._audience("https://n.example") == "https://n.example"
    assert seen == [
        "https://n.example/api/cache-config?issuer=https%3A%2F%2Ftoken.actions.githubusercontent.com"
    ]

    def empty(url: str, timeout: float = 0) -> io.BytesIO:
        return io.BytesIO(b"{}")

    monkeypatch.setattr(stream.urllib.request, "urlopen", empty)
    assert stream._audience("https://n.example") == ""


def test_niks3_oidc_without_audience_disables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://gh.example/token")
    monkeypatch.setattr(stream, "_audience", lambda _: "")
    assert ready(Niks3("https://n.example", "")) is False
    assert "::warning::niks3 disabled" in capsys.readouterr().out


def test_niks3_without_token_or_oidc_disables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    assert ready(Niks3("https://n.example", "")) is False
    assert "id-token" in capsys.readouterr().out


def test_push_batches_and_isolates_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Fake:
        name = "Fake"
        batches: ClassVar[list[int]] = []

        def setup(self) -> None:
            pass

        def push(self, paths: list[str]) -> None:
            self.batches.append(len(paths))

    fake = Fake()
    push(fake, [f"/nix/store/{i}" for i in range(2500)])
    assert fake.batches == [1000, 1000, 500]

    class Boom:
        name = "Boom"

        def setup(self) -> None:
            pass

        def push(self, paths: list[str]) -> None:
            raise RuntimeError("nope")

    push(Boom(), ["/nix/store/aaa-x"])
    assert "::warning::Boom push failed" in capsys.readouterr().out


def test_audience_requires_https() -> None:
    with pytest.raises(RuntimeError):
        stream._audience("http://n.example")


def test_mint_script_neutralizes_hostile_audience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream.tempfile, "tempdir", str(tmp_path))
    hostile = 'x" + 1 + "'
    body = Path(stream._mint(hostile)).read_text()
    ast.parse(body)
    assert hostile not in body
    assert "x%22%20%2B%201%20%2B%20%22" in body


def test_push_continues_after_failed_batch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.name = "Flaky"
            self.attempts = 0

        def setup(self) -> None:
            pass

        def push(self, paths: list[str]) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient")

    flaky = Flaky()
    push(flaky, [f"/nix/store/{i}" for i in range(1500)])
    assert flaky.attempts == 2
    assert capsys.readouterr().out.count("::warning::Flaky push failed") == 1


def test_ready_reports_working_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch)
    monkeypatch.setenv("CACHIX_AUTH_TOKEN", "t")
    assert ready(Cachix("d")) is True
