#!/bin/sh
# post-build hook, runs as root under the nix daemon
# append every built output path to the spool and never fail the build loop
# the env override exists for tests, the daemon never sets ATELIER_SPOOL
# OUT_PATHS is deliberately unquoted so word splitting yields one path per line
# set -f disables globbing so a metacharacter in a path never expands
# an empty OUT_PATHS writes a blank line, the spool reader skips blank lines
set -f
{ printf '%s\n' $OUT_PATHS >> "${ATELIER_SPOOL:-/nix/var/atelier/spool}"; } 2>/dev/null || true
