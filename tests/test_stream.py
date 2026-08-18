import ast
import io
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from atelier import stream
from atelier.stream import (
    Attic,
    Cachix,
    Niks3,
    backends,
    mode_final,
    mode_stream,
    push,
    read_new,
    ready,
    stream_backend,
)


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


class _Sink:
    def __init__(self, fail_setup: bool = False) -> None:
        self.name = "Sink"
        self.fail_setup = fail_setup
        self.setups = 0
        self.pushed: list[str] = []
        self.idents: list[int] = []

    def setup(self) -> None:
        self.setups += 1
        if self.fail_setup:
            raise RuntimeError("no")

    def push(self, paths: list[str]) -> None:
        self.idents.append(threading.get_ident())
        self.pushed.extend(paths)


def test_stream_backend_drains_then_exits_on_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    done = tmp_path / "done"
    spool.write_text("/nix/store/aaa-x\n/nix/store/bbb-y\n")
    done.touch()
    sink = _Sink()
    monkeypatch.setattr(stream.time, "sleep", lambda _: pytest.fail("loop did not exit"))
    stream_backend(sink, spool, done)
    assert sink.pushed == ["/nix/store/aaa-x", "/nix/store/bbb-y"]


def test_stream_backend_exits_when_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spool = tmp_path / "spool"
    done = tmp_path / "done"
    spool.write_text("/nix/store/aaa-x\n")
    sink = _Sink(fail_setup=True)
    # no sentinel, the disabled backend must still return
    monkeypatch.setattr(stream.time, "sleep", lambda _: pytest.fail("loop did not exit"))
    stream_backend(sink, spool, done)
    assert sink.pushed == []
    assert "Sink disabled" in capsys.readouterr().out


def test_mode_stream_runs_one_thread_per_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    done = tmp_path / "done"
    spool.write_text("/nix/store/aaa-x\n")
    done.touch()
    sinks = [_Sink(), _Sink()]
    monkeypatch.setattr(stream, "backends", lambda: list(sinks))
    mode_stream(spool, done)
    assert sinks[0].pushed == ["/nix/store/aaa-x"]
    assert sinks[1].pushed == ["/nix/store/aaa-x"]
    # one real thread per backend, neither on the caller's thread
    idents = {sinks[0].idents[0], sinks[1].idents[0]}
    assert len(idents) == 2
    assert threading.get_ident() not in idents


def test_mode_stream_without_backends_warns_and_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stream, "backends", list)
    mode_stream(tmp_path / "spool", tmp_path / "done")
    assert "No cache backend configured" in capsys.readouterr().out


def test_stream_backend_streams_across_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    done = tmp_path / "done"
    spool.write_text("")
    sink = _Sink()
    ticks: list[int] = []

    def tick(_: float) -> None:
        ticks.append(1)
        if len(ticks) == 1:
            spool.write_text("/nix/store/aaa-x\n")
        elif len(ticks) == 2:
            spool.write_text("/nix/store/aaa-x\n/nix/store/bbb-y\n")
            done.touch()
        elif len(ticks) > 5:
            pytest.fail("loop did not terminate")

    monkeypatch.setattr(stream.time, "sleep", tick)
    stream_backend(sink, spool, done)
    assert sink.pushed == ["/nix/store/aaa-x", "/nix/store/bbb-y"]
    assert sink.setups == 1


def test_stream_backend_warns_when_reader_dies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(spool: Path, offset: int) -> tuple[list[str], int]:
        raise PermissionError("spool unreadable")

    monkeypatch.setattr(stream, "read_new", boom)
    stream_backend(_Sink(), tmp_path / "spool", tmp_path / "done")
    assert "::warning::Sink streamer stopped" in capsys.readouterr().out


def test_mode_final_dedups_and_pushes_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n/nix/store/aaa-x\n/nix/store/bbb-y\n")
    monkeypatch.delenv("BUILD_OUTCOME", raising=False)
    sink = _Sink()
    monkeypatch.setattr(stream, "backends", lambda: [sink])
    mode_final(spool, tmp_path / "done", tmp_path / "pid", tmp_path / "log")
    assert sink.pushed == ["/nix/store/aaa-x", "/nix/store/bbb-y"]
    assert (tmp_path / "done").exists()


def test_mode_final_appends_outputs_only_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n")
    monkeypatch.setenv("BUILD_OUTCOME", "success")
    monkeypatch.setenv("INSTALLABLE", ".#pkg")
    seen: list[str] = []

    def outputs(installable: str) -> list[str]:
        seen.append(installable)
        return ["/nix/store/aaa-x", "/nix/store/fff-out"]

    monkeypatch.setattr(stream, "_outputs", outputs)
    sink = _Sink()
    monkeypatch.setattr(stream, "backends", lambda: [sink])
    mode_final(spool, tmp_path / "done", tmp_path / "pid", tmp_path / "log")
    assert seen == [".#pkg"]
    # finals overlap the spool when the top drv was built locally, dedup holds
    assert sink.pushed == ["/nix/store/aaa-x", "/nix/store/fff-out"]


def test_mode_final_failure_outcome_skips_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    spool.write_text("/nix/store/aaa-x\n")
    monkeypatch.setenv("BUILD_OUTCOME", "failure")

    def boom(installable: str) -> list[str]:
        raise AssertionError("must not resolve outputs on failure")

    monkeypatch.setattr(stream, "_outputs", boom)
    sink = _Sink()
    monkeypatch.setattr(stream, "backends", lambda: [sink])
    mode_final(spool, tmp_path / "done", tmp_path / "pid", tmp_path / "log")
    assert sink.pushed == ["/nix/store/aaa-x"]


def test_mode_final_empty_set_pushes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUILD_OUTCOME", raising=False)
    called = False

    def probe() -> list[stream.Backend]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(stream, "backends", probe)
    mode_final(
        tmp_path / "spool", tmp_path / "done", tmp_path / "pid", tmp_path / "log"
    )
    assert called is False


def test_mode_final_replays_streamer_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "log").write_text("hello from the streamer\n")
    monkeypatch.delenv("BUILD_OUTCOME", raising=False)
    monkeypatch.setattr(stream, "backends", list)
    mode_final(
        tmp_path / "spool", tmp_path / "done", tmp_path / "pid", tmp_path / "log"
    )
    out = capsys.readouterr().out
    assert "::group::Streamer log" in out
    assert "hello from the streamer" in out
    assert "::endgroup::" in out


def test_mode_final_waits_for_dead_pid_instantly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a pid that cannot exist, kill(pid, 0) raises and the wait returns
    (tmp_path / "pid").write_text("99999999")
    monkeypatch.delenv("BUILD_OUTCOME", raising=False)
    monkeypatch.setattr(stream, "backends", list)
    mode_final(
        tmp_path / "spool", tmp_path / "done", tmp_path / "pid", tmp_path / "log"
    )


def test_cli_exits_zero_without_backends(tmp_path: Path) -> None:
    # run by file path exactly as the workflow does
    env = {"RUNNER_TEMP": str(tmp_path), "ATELIER_SPOOL": str(tmp_path / "spool")}
    for mode in ("stream", "final"):
        proc = subprocess.run(
            [sys.executable, stream.__file__, "--mode", mode],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


def test_outputs_resolves_and_surfaces_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []

    def ok(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, stdout="/nix/store/fff-out\n\n", stderr=""
        )

    monkeypatch.setattr(stream.subprocess, "run", ok)
    assert stream._outputs(".#pkg") == ["/nix/store/fff-out"]
    assert seen == [["nix", "build", ".#pkg^*", "--no-link", "--print-out-paths"]]

    def boom(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv, stderr="error: attribute missing")

    monkeypatch.setattr(stream.subprocess, "run", boom)
    assert stream._outputs(".#pkg") == []
    out = capsys.readouterr().out
    assert "error: attribute missing" in out
    assert "::warning::Final output resolution failed" in out


def test_wait_warns_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # a live pid with a zero budget takes the timeout path immediately
    (tmp_path / "pid").write_text(str(os.getpid()))
    monkeypatch.setattr(stream, "WAIT", 0.0)
    stream._wait(tmp_path / "pid")
    assert "Streamer still running" in capsys.readouterr().out


def test_replay_tolerates_bad_bytes_and_missing_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log"
    log.write_bytes(b"partial \xff line without newline")
    stream._replay(log)
    out = capsys.readouterr().out
    assert out.startswith("::group::Streamer log\n")
    assert out.endswith("\n::endgroup::\n")


def test_cli_missing_runner_temp_warns_and_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, stream.__file__, "--mode", "final"],
        env={},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "::warning::Cache push failed" in proc.stdout


def test_cli_final_replays_log_and_touches_sentinel(tmp_path: Path) -> None:
    # pins the RUNNER_TEMP file names build.yaml writes
    (tmp_path / "atelier-stream.log").write_text("streamer said hi\n")
    proc = subprocess.run(
        [sys.executable, stream.__file__, "--mode", "final"],
        env={"RUNNER_TEMP": str(tmp_path), "ATELIER_SPOOL": str(tmp_path / "spool")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "streamer said hi" in proc.stdout
    assert (tmp_path / "atelier-stream.done").exists()


def test_main_uses_the_runner_temp_names_build_yaml_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Path] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(stream, "mode_final", lambda *a: seen.extend(a))
    monkeypatch.setattr(sys, "argv", ["stream.py", "--mode", "final"])
    assert stream.main() == 0
    assert [p.name for p in seen[1:]] == [
        "atelier-stream.done",
        "atelier-stream.pid",
        "atelier-stream.log",
    ]
