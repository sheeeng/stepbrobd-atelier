import fnmatch
import posixpath
import tomllib
from pathlib import Path
from typing import Any

from atelier.types import (
    DEFAULT_INCLUDE,
    DEFAULT_SYSTEMS,
    MAX_RECURSE_DEPTH,
    NIXOS_CACHE,
    PER_SYSTEM_SETS,
    Rules,
)

_GLOB_CHARS = frozenset("*?[]")


def _normalize_root(root: object) -> str:
    """Normalize a validated repository-relative flake directory."""
    if not isinstance(root, str):
        raise TypeError("root must be a string")
    if not root:
        raise ValueError("root must not be empty")
    if posixpath.isabs(root):
        raise ValueError("root must be relative to the repository")

    normalized = posixpath.normpath(root)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("root must stay within the repository")
    return normalized


def _build(data: dict[str, Any]) -> Rules:
    """Build ``Rules`` from parsed toml, falling back to defaults for omitted keys.

    The official cache is always folded into ``substituters`` (a set, so a user
    who also lists it does not duplicate it), so cache-status checks work even
    when the rule file names no cache of its own.
    """
    return Rules(
        systems=tuple(data.get("systems", DEFAULT_SYSTEMS)),
        include=tuple(data.get("include", DEFAULT_INCLUDE)),
        exclude=tuple(data.get("exclude", ())),
        substituters=frozenset(data.get("substituters", ())) | {NIXOS_CACHE},
        trusted_public_keys=frozenset(data.get("trusted-public-keys", ())),
        root=_normalize_root(data.get("root", ".")),
    )


def load(path: Path) -> Rules:
    """Read a rule file, falling back to defaults for any omitted key."""
    return _build(tomllib.loads(path.read_text()))


def defaults() -> Rules:
    """The rules used when no rule file exists, every key at its built-in default.

    Identical to loading an empty rule file, so a missing ``atelier.toml`` and an
    empty one evaluate the same flake.
    """
    return _build({})


def _match_segments(pat: list[str], seg: list[str]) -> bool:
    """Match a list of glob segments against a list of path segments.

    A bare ``*`` spans exactly one segment; a ``**`` spans zero or more whole
    segments (so the match backtracks over how many it absorbs). Within a single
    segment ``fnmatch`` metacharacters apply as usual.
    """
    if not pat:
        return not seg
    head, *rest = pat
    if head == "**":
        # ``**`` consumes zero segments here, or one and stays to absorb more
        return _match_segments(rest, seg) or (
            bool(seg) and _match_segments(pat, seg[1:])
        )
    return (
        bool(seg)
        and fnmatch.fnmatchcase(seg[0], head)
        and _match_segments(rest, seg[1:])
    )


def matches(pattern: str, path: str) -> bool:
    """Match a dotted glob against a flake attribute path.

    Matching is segment wise, so a bare ``*`` spans exactly one segment and a
    nested scope needs its own segment. Thus ``legacyPackages.*.*`` matches
    ``legacyPackages.x86_64-linux.caddy`` but not
    ``legacyPackages.x86_64-linux.ocamlPackages.dune``, which needs the explicit
    ``legacyPackages.*.ocamlPackages.*``. A ``**`` segment spans any depth, so
    ``legacyPackages.*.**`` matches a package at any nesting under a system.
    """
    return _match_segments(pattern.split("."), path.split("."))


def include_max_depth(rules: Rules) -> int:
    """Deepest attribute path the include globs can match, for the eval recursion.

    A ``**`` segment matches arbitrarily deep, so a pattern containing one raises
    the depth to the hard cap. Returns at least 1 so an empty include never asks
    for a zero or negative recursion budget.
    """
    best = 1
    for pattern in rules.include:
        segments = pattern.split(".")
        depth = MAX_RECURSE_DEPTH if "**" in segments else len(segments)
        best = max(best, depth)
    return min(best, MAX_RECURSE_DEPTH)


def included(path: str, rules: Rules) -> bool:
    """True when `path` matches any include glob."""
    return any(matches(pattern, path) for pattern in rules.include)


def excluded(path: str, rules: Rules) -> bool:
    """True when `path` matches any exclude glob."""
    return any(matches(pattern, path) for pattern in rules.exclude)


def prunable_excludes(rules: Rules) -> dict[str, dict[str, dict[str, Any]]]:
    """Exclude trees droppable before evaluation, grouped by set then system.

    Handles ``<set>.<sys>.<path...>`` excludes where ``<sys>`` is ``*`` (all
    systems) or a literal system and every path segment is literal (no glob
    metacharacters), at any depth. A trailing ``**`` also qualifies because the
    parent it names is an attrset and never buildable itself, so dropping the
    parent removes exactly the leaves the glob would drop after evaluation. The
    select expression ``removeAttrs`` the marked names while it recurses, so
    nix-eval-jobs never forces, fetches, or builds an excluded attribute. Any
    other glob stays a post-eval filter.

    Shape: ``{set: {("*" | system): tree}}`` where a tree maps an attribute name
    to ``True`` (drop it and everything below) or to a subtree to descend into.
    ``True`` wins over a subtree when rules overlap, since dropping the parent
    already drops every descendant.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for pattern in rules.exclude:
        parts = pattern.split(".")
        if len(parts) < 3 or parts[0] not in PER_SYSTEM_SETS:
            continue
        system = parts[1]
        if system != "*" and _GLOB_CHARS & set(system):
            continue
        path = parts[2:]
        # a trailing ** means drop the parent attrset wholesale
        # a bare "<set>.<sys>.**" is left to the post filter, dropping the whole
        # system root would mean not rooting it at all
        if path[-1] == "**":
            path = path[:-1]
        if not path or any(_GLOB_CHARS & set(seg) for seg in path):
            continue
        node = out.setdefault(parts[0], {}).setdefault(system, {})
        for seg in path[:-1]:
            nxt = node.get(seg)
            if nxt is True:
                # an ancestor is already dropped, the deeper rule is redundant
                break
            if not isinstance(nxt, dict):
                nxt = node[seg] = {}
            node = nxt
        else:
            node[path[-1]] = True
    return out
