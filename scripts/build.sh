#!/bin/sh
# Thin wrapper so CI, docs and humans invoke the build the same way.
# The build needs root: it creates an Alpine chroot and bind-mounts /dev and
# /proc into it. On a disposable build host that is simply:
#   sudo scripts/build.sh --arch x86_64
set -eu
# shellcheck disable=SC1007  # CPATH= applies to cd only, so it must stay empty
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/build.py" "$@"
