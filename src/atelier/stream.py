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

import subprocess
from pathlib import Path

# paths per tool invocation, keeps argv far under ARG_MAX on both platforms
BATCH = 1000
# seconds between spool polls when idle
POLL = 2.0
# seconds final mode waits for the streamer to drain before proceeding
WAIT = 900.0


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
