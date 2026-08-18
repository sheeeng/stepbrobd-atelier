"""Streaming cache push for build cells.

Run by file path with the runner's system python3, never as part of the
packaged tool, so this module stays stdlib-only and keeps Python 3.12
syntax (ubuntu runners ship 3.12, the package floor of 3.14 applies only
to the tool run via nix).

Stream mode tails the spool the post-build hook appends to and pushes new
store paths to every configured backend while the build runs. Final mode
stops the streamer, then pushes every spooled path plus, when the build
succeeded, the final outputs' closure. Both modes exit 0 unconditionally
because a cache push never fails a build.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# paths per tool invocation, keeps argv far under ARG_MAX on both platforms
BATCH = 1000
# seconds between spool and pid polls when idle
POLL = 2.0
# seconds final mode waits for the streamer to drain before proceeding
WAIT = 900.0

# github's oidc issuer, the audience is discovered from the niks3 server
ISSUER = "https://token.actions.githubusercontent.com"

# the mint script niks3 re-runs to refresh its token
# expires_at of now+240 keeps the refresh inside github's ~5 minute token life
_MINT = """#!/usr/bin/env python3
import json, os, time, urllib.request
req = urllib.request.Request(
    os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=@AUDIENCE@",
    headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
)
with urllib.request.urlopen(req) as resp:
    token = json.load(resp)["value"]
exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 240))
print(json.dumps({"token": token, "expires_at": exp}))
"""


def _warn(msg: str) -> None:
    # single write so concurrent threads cannot interleave annotations
    # stdout on purpose, the streamer's stdout is the log final mode replays
    print(f"::warning::{msg}\n", end="", flush=True)


def _run(
    argv: list[str], stdin: str | None = None, env: dict[str, str] | None = None
) -> None:
    # module level so tests can monkeypatch process execution
    # argv list only, nothing is interpreted by a shell
    # env is a complete environment, not an overlay over os.environ
    subprocess.run(argv, input=stdin, env=env, check=True, text=True)


def read_new(spool: Path, offset: int) -> tuple[list[str], int]:
    """Complete lines past a byte offset and the new byte offset.

    A partial trailing line stays for the next read, blank lines are
    skipped, and a missing spool reads as empty. The spool is append
    only; the offset never rewinds. Undecodable bytes are replaced so
    a corrupt line degrades to a failed push instead of a dead reader.
    """
    try:
        data = spool.read_bytes()[offset:]
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[: end + 1]
    lines = [ln.strip() for ln in chunk.decode(errors="replace").split("\n")]
    return [ln for ln in lines if ln], offset + len(chunk)


class Backend(Protocol):
    """One binary cache target, structurally satisfied by the classes below."""

    name: str

    def setup(self) -> None: ...

    def push(self, paths: list[str]) -> None: ...


def _ensure(binary: str, installable: str) -> None:
    # lix only has 'install', on cppnix it is a deprecated alias for 'add'
    # revert to 'add' once lix supports 'add'
    if shutil.which(binary) is None:
        _run(["nix", "profile", "install", installable])


def _secret(token: str) -> str:
    # mkstemp creates 0600, the token never lands in argv or the log
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return path


def _audience(server: str) -> str:
    # https only, this fetch decides whether to trust the server with a token
    if not server.startswith("https://"):
        raise RuntimeError("server URL must be https")
    query = urllib.parse.urlencode({"issuer": ISSUER})
    with urllib.request.urlopen(
        f"{server.rstrip('/')}/api/cache-config?{query}", timeout=30
    ) as resp:
        return str(json.load(resp).get("oidc_audience") or "")


def _mint(audience: str) -> str:
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        # percent-encode so a hostile audience cannot escape the string
        # literal in the generated script or smuggle extra query parameters
        f.write(_MINT.replace("@AUDIENCE@", urllib.parse.quote(audience, safe="")))
    os.chmod(path, 0o700)
    return path


@dataclass
class Attic:
    server: str
    cache: str
    token: str = field(repr=False)
    name: str = "Attic"

    def setup(self) -> None:
        if not self.token:
            raise RuntimeError("ATTIC_TOKEN is not set")
        _ensure("attic", "nixpkgs#attic-client")
        try:
            _run(["attic", "login", "default", self.server, self.token])
        except subprocess.CalledProcessError as e:
            # the token is in argv, re-raise without echoing it into the log
            raise RuntimeError(
                f"attic login failed with exit code {e.returncode}"
            ) from None
        # an unreachable cache disables the backend, never the build
        _run(["attic", "cache", "info", self.cache])

    def push(self, paths: list[str]) -> None:
        _run(["attic", "push", self.cache, *paths])


@dataclass
class Cachix:
    cache: str
    name: str = "Cachix"

    def setup(self) -> None:
        # pushing needs credentials, fail once here instead of every batch
        if not os.environ.get("CACHIX_AUTH_TOKEN") and not os.environ.get(
            "CACHIX_SIGNING_KEY"
        ):
            raise RuntimeError("CACHIX_AUTH_TOKEN is not set")
        _ensure("cachix", "nixpkgs#cachix")

    def push(self, paths: list[str]) -> None:
        env = dict(os.environ)
        # cachix rejects an empty signing key, absent means unsigned
        if not env.get("CACHIX_SIGNING_KEY"):
            env.pop("CACHIX_SIGNING_KEY", None)
        _run(["cachix", "push", self.cache], stdin="\n".join(paths) + "\n", env=env)


@dataclass
class Niks3:
    server: str
    token: str = field(repr=False)
    name: str = "niks3"
    auth: tuple[str, str] | None = None

    def setup(self) -> None:
        # nixpkgs still lags at 1.4.0 without the auth flags, use the pin
        _ensure("niks3", "github:stepbrobd/inc#niks3")
        if self.token:
            self.auth = ("--auth-token-path", _secret(self.token))
        elif os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
            audience = _audience(self.server)
            if not audience:
                raise RuntimeError(
                    "server advertises no oidc_audience for the GitHub issuer"
                )
            self.auth = ("--auth-token-script", _mint(audience))
        else:
            raise RuntimeError("OIDC needs 'id-token: write' in the caller workflow")

    def push(self, paths: list[str]) -> None:
        if self.auth is None:
            raise RuntimeError("niks3 setup did not run")
        _run(["niks3", "push", "--server-url", self.server, *self.auth, *paths])


def backends() -> list[Backend]:
    env = os.environ
    out: list[Backend] = []
    if env.get("ATTIC_SERVER") and env.get("ATTIC_CACHE"):
        out.append(
            Attic(env["ATTIC_SERVER"], env["ATTIC_CACHE"], env.get("ATTIC_TOKEN", ""))
        )
    if env.get("CACHIX_CACHE"):
        out.append(Cachix(env["CACHIX_CACHE"]))
    if env.get("NIKS3_SERVER"):
        out.append(Niks3(env["NIKS3_SERVER"], env.get("NIKS3_TOKEN", "")))
    return out


def ready(backend: Backend) -> bool:
    # one time setup, a failure disables the backend for this run
    try:
        backend.setup()
    except Exception as e:  # noqa: BLE001
        _warn(f"{backend.name} disabled: {e}")
        return False
    return True


def push(backend: Backend, paths: list[str]) -> None:
    # per batch so one failure cannot abandon the rest, this same function
    # is the final drain's last line of defense
    for i in range(0, len(paths), BATCH):
        try:
            backend.push(paths[i : i + BATCH])
        except Exception as e:  # noqa: BLE001
            _warn(f"{backend.name} push failed: {e}")


# tool installs share one nix profile, serialize the setups
_SETUP = threading.Lock()


def stream_backend(backend: Backend, spool: Path, done: Path) -> None:
    # own offset per backend, exits once the sentinel exists and every
    # complete line has been read, or immediately when setup fails
    offset = 0
    ok: bool | None = None
    try:
        while True:
            lines, offset = read_new(spool, offset)
            if lines:
                if ok is None:
                    with _SETUP:
                        ok = ready(backend)
                if not ok:
                    return
                push(backend, lines)
            elif done.exists():
                return
            else:
                time.sleep(POLL)
    except Exception as e:  # noqa: BLE001
        # a dead reader is invisible otherwise, final mode re-pushes the spool
        _warn(f"{backend.name} streamer stopped: {e}")


def mode_stream(spool: Path, done: Path) -> None:
    bs = backends()
    if not bs:
        _warn("No cache backend configured, streamer exiting")
        return
    threads = [
        threading.Thread(target=stream_backend, args=(b, spool, done)) for b in bs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _wait(pidfile: Path) -> None:
    # bounded wait, a hung streamer only costs duplicate bandwidth
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(POLL)
    _warn("Streamer still running after wait timeout, pushing anyway")


def _replay(log: Path) -> None:
    if not log.exists():
        return
    text = log.read_text(errors="replace")
    print("::group::Streamer log")
    sys.stdout.write(text)
    # github only parses commands at line start, keep the group closable
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    print("::endgroup::", flush=True)


def _outputs(installable: str) -> list[str]:
    # the final outputs, resolvable only after a successful build
    if not installable:
        return []
    try:
        proc = subprocess.run(
            ["nix", "build", f"{installable}^*", "--no-link", "--print-out-paths"],
            check=True,
            text=True,
            capture_output=True,
            timeout=WAIT,
        )
    except Exception as e:  # noqa: BLE001
        # str(CalledProcessError) omits the captured stderr, print it too
        stderr = str(getattr(e, "stderr", "") or "").strip()
        if stderr:
            print(stderr[-2000:], flush=True)
        _warn(f"Final output resolution failed: {e}")
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def mode_final(spool: Path, done: Path, pidfile: Path, log: Path) -> None:
    done.touch()
    try:
        _wait(pidfile)
        _replay(log)
    except Exception as e:  # noqa: BLE001
        # a preamble failure must never cost the drain itself
        _warn(f"Streamer wait or log replay failed: {e}")
    paths = read_new(spool, 0)[0]
    if os.environ.get("BUILD_OUTCOME") == "success":
        installable = os.environ.get("INSTALLABLE", "")
        if not installable:
            _warn("INSTALLABLE is not set, skipping the final closure push")
        # the closure sweep also covers substituted deps missing on the cache
        paths += _outputs(installable)
    paths = list(dict.fromkeys(paths))
    if not paths:
        return
    for b in backends():
        if ready(b):
            push(b, paths)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("stream", "final"), required=True)
    mode = ap.parse_args().mode
    spool = Path(os.environ.get("ATELIER_SPOOL", "/nix/var/atelier/spool"))
    tmp = Path(os.environ["RUNNER_TEMP"])
    done = tmp / "atelier-stream.done"
    if mode == "stream":
        mode_stream(spool, done)
    else:
        mode_final(spool, done, tmp / "atelier-stream.pid", tmp / "atelier-stream.log")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        # a cache push must never fail the build, even on an internal bug
        _warn(f"Cache push failed: {e!r}")
        sys.exit(0)
