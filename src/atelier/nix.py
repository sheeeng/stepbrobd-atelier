import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from typing import Any

from atelier.types import (
    CONFIG_SETS,
    LEAF_SETS,
    MAX_RECURSE_DEPTH,
    PER_SYSTEM_SETS,
    RUNNERS,
    SCOPE_DENYLIST,
    SKIP_PATTERN,
    Job,
)

_SKIP_RE = re.compile(SKIP_PATTERN, re.IGNORECASE)

# quoted placeholders keep select.nix parseable before substitution
# generated literals contain only allowlisted or escaped values
_SELECT_TEMPLATE = (resources.files("atelier") / "select.nix").read_text()


def _nix_str(value: str) -> str:
    """Quote a value as a nix string literal, escaping injection vectors.

    Exclude leaf names come from the rule file, so escape backslashes, quotes,
    and the ``${`` interpolation opener before embedding them in the expression.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def _nix_list(items: Sequence[str]) -> str:
    return " ".join(_nix_str(item) for item in items)


def _merge_trees(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Merge two exclude trees, ``True`` beating a subtree on overlapping names.

    Dropping a parent already drops every descendant, so a ``True`` from either
    side must survive a subtree from the other.
    """
    out: dict[str, Any] = dict(base)
    for name, sub in extra.items():
        cur = out.get(name)
        if sub is True or cur is True:
            out[name] = True
        elif cur is None:
            out[name] = sub
        else:
            out[name] = _merge_trees(cur, sub)
    return out


def _nix_tree(tree: Mapping[str, Any]) -> str:
    """Render an exclude tree as a nix attrset literal, names escaped and sorted."""
    pairs = " ".join(
        f"{_nix_str(name)} = {'true' if sub is True else _nix_tree(sub)};"
        for name, sub in sorted(tree.items())
    )
    return f"{{ {pairs} }}" if pairs else "{ }"


def _nix_excludes(
    excludes: Mapping[str, Mapping[str, Mapping[str, Any]]], systems: Sequence[str]
) -> str:
    """Render exclude trees as a Nix attrset.

    Per system sets contain one tree per requested system after folding ``"*"``.
    Configuration sets contain the flat ``"*"`` host tree.
    """
    sets = []
    for set_name, by_system in sorted(excludes.items()):
        star = by_system.get("*", {})
        if set_name in CONFIG_SETS:
            if star:
                sets.append(f"{_nix_str(set_name)} = {_nix_tree(star)};")
            continue
        pairs = []
        for system in systems:
            tree = _merge_trees(by_system.get(system, {}), star)
            if tree:
                pairs.append(f"{_nix_str(system)} = {_nix_tree(tree)};")
        if pairs:
            sets.append(f"{_nix_str(set_name)} = {{ {' '.join(pairs)} }};")
    rendered = " ".join(sets)
    return f"{{ {rendered} }}" if rendered else "{ }"


def _build_select(
    systems: Sequence[str],
    per_system_sets: Sequence[str],
    config_sets: Sequence[str],
    leaf_sets: Sequence[str] = (),
    excludes: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    max_depth: int = MAX_RECURSE_DEPTH,
) -> str:
    # revalidate discovery values before embedding them in the expression
    for system in systems:
        if system not in RUNNERS:
            raise ValueError(f"unknown system {system!r}")
    for output in (*per_system_sets, *config_sets, *leaf_sets):
        if (
            output not in PER_SYSTEM_SETS
            and output not in CONFIG_SETS
            and output not in LEAF_SETS
        ):
            raise ValueError(f"unknown output set {output!r}")
    trees = excludes or {}
    # set names are allowlisted and attribute names are escaped
    for output in trees:
        if output not in PER_SYSTEM_SETS and output not in CONFIG_SETS:
            raise ValueError(f"unknown output set {output!r}")
    return (
        _SELECT_TEMPLATE.replace('"@SYSTEMS@"', _nix_list(systems))
        .replace('"@PER_SYSTEM@"', _nix_list(per_system_sets))
        .replace('"@CONFIG@"', _nix_list(config_sets))
        .replace('"@LEAF@"', _nix_list(leaf_sets))
        # max_depth is an integer clamped to the hard cap
        .replace('"@MAXDEPTH@"', str(min(int(max_depth), MAX_RECURSE_DEPTH)))
        .replace('"@DENYLIST@"', _nix_list(SCOPE_DENYLIST))
        # replace arbitrary names last so names matching tokens stay literal
        .replace('"@EXCLUDES@"', _nix_excludes(trees, systems))
    )


def _eval_command(
    flake: str, select: str, workers: int, substituters: Iterable[str]
) -> list[str]:
    """The nix-eval-jobs argv, checking cache status against `substituters`.

    `--check-cache-status` tags each attribute with whether its outputs are
    already in a queried cache (a `cacheStatus` field), so discovery can skip
    building cached ones. The caches are sorted for a stable command, and
    `require-sigs` is disabled because an existence check only asks whether the
    path is in the cache, not whether this host trusts the cache's signing key:
    the runner never imports these paths, and an untrusted hit would otherwise be
    ignored (nix-eval-jobs reads no flake `nixConfig`, so the keys are unknown).
    """
    cmd = [
        "nix",
        "run",
        "nixpkgs#nix-eval-jobs",
        "--",
        "--flake",
        flake,
        "--force-recurse",
        "--check-cache-status",
        "--workers",
        str(workers),
    ]
    caches = sorted(substituters)
    if caches:
        cmd += [
            "--option",
            "extra-substituters",
            " ".join(caches),
            "--option",
            "require-sigs",
            "false",
        ]
    cmd += ["--select", select]
    return cmd


def evaluate(
    flake: str,
    systems: Sequence[str],
    per_system_sets: Sequence[str],
    config_sets: Sequence[str],
    leaf_sets: Sequence[str] = (),
    workers: int = 4,
    excludes: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    substituters: Iterable[str] = (),
    max_depth: int = MAX_RECURSE_DEPTH,
) -> list[dict[str, Any]]:
    """Run nix-eval-jobs over the rooted output sets and return one object per attr.

    Per attribute eval errors are reported inline as objects carrying an `error`
    field and never abort the run. A non zero exit is a fatal evaluation failure
    of the whole flake and is raised. `excludes` holds the exclude trees pruned
    during recursion so a marked attribute is never evaluated, fetched, or built.
    `substituters` are the caches each attribute's cache status is checked against.
    `max_depth` bounds the per system scope recursion to the include globs' depth,
    so a re-exported package set is not force-recursed into the whole nixpkgs lib.
    """
    select = _build_select(
        systems, per_system_sets, config_sets, leaf_sets, excludes, max_depth
    )
    cmd = _eval_command(flake, select, workers, substituters)
    # capture stdout (the json results) but let stderr stream to the log live,
    # so fetches, getFlake calls, and per-attr eval progress are visible instead
    # of buffered until the end, where a slow eval looks like a frozen run
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"nix-eval-jobs failed for {flake!r} (see the log above)")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def to_job(obj: dict[str, Any], flake: str = ".") -> Job:
    """Normalise one nix-eval-jobs object into a `Job`.

    The rooted key carries the set and system needed to reconstruct the full
    attribute path. `flake` is the checkout-relative reference used by emitted
    build installables.
    """
    path = ".".join(obj.get("attrPath") or [])
    drv = obj.get("drvPath")
    error = obj.get("error")
    # "cached" (set by --check-cache-status) means every output is in a queried
    # binary cache. "local" (this runner's store only) and "notBuilt" are not
    # cross-runner safe, so only an outright "cached" is treated as cached.
    cached = obj.get("cacheStatus") == "cached"
    set_name = path.split(".")[0] if path else ""

    if set_name in CONFIG_SETS:
        system = obj.get("system") or ""
        installable = f"{flake}#{path}.config.system.build.toplevel" if drv else ""
    else:
        segments = path.split(".")
        system = segments[1] if len(segments) > 1 else (obj.get("system") or "")
        installable = f"{flake}#{path}" if drv else ""

    return Job(
        path=path, system=system, installable=installable, error=error, cached=cached
    )


def clean_error(error: str) -> str:
    """Collapse a nix eval error to a single readable line.

    Nix wraps the actionable message in assert boilerplate, so keep the text
    after the last `error:` marker where the real reason lives.
    """
    flat = " ".join(error.split())
    parts = flat.split("error:")
    message = f"error: {parts[-1].strip()}" if len(parts) > 1 else flat
    # neutralise github workflow-command markers in attacker controlled text
    # so a crafted eval error cannot spoof annotations when printed in a cell;
    # collapse every run of colons since a lone replace leaves "::" on odd runs
    return re.sub(r":{2,}", ":", message.strip())[:400]


def is_skippable(error: str) -> bool:
    """True when an eval error denotes an expected unbuildable attribute."""
    return _SKIP_RE.search(error) is not None
