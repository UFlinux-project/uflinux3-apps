#!/usr/bin/env python3
"""Sort downloaded build artifacts into the per-architecture spool.

The compile job uploads one artifact per (architecture, package) shard, named
"apk-<arch>-<package>". An APK filename itself carries no architecture - abuild
records it in the directory above the file, which artifacts flatten away - so
the architecture is taken from the artifact directory that download-artifact
creates. The APKs are written back out as out/packages/<arch>/*.apk, which is
what "build.py --stage index" expects.

The same filename is expected to turn up under several architectures: anicraft is
built once per architecture and named the same every time, and abuild builds
-doc and the completion subpackages as noarch, which the build stage collects
into the building architecture's own directory so that every architecture's
directory is self-contained. Architectures are therefore kept strictly apart
here; only two artifacts for the *same* architecture disagreeing about one name
is an error.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ARCHES = sorted(json.loads((PROJECT / "config/architectures.json").read_text()),
                key=len, reverse=True)
PREFIX = "apk-"


def architecture(name):
    """Return the architecture an artifact directory name refers to."""
    if not name.startswith(PREFIX):
        return None
    remainder = name[len(PREFIX):]
    return next((arch for arch in ARCHES if remainder.startswith(arch + "-")), None)


def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def collect(spool, spooled):
    """Copy every artifact's APKs into <spooled>/<arch>, and report what was found."""
    found = 0
    architectures = set()
    for artifact in sorted(p for p in spool.iterdir() if p.is_dir()):
        arch = architecture(artifact.name)
        if arch is None:
            raise SystemExit("unexpected artifact name: " + artifact.name)
        target = spooled / arch
        target.mkdir(parents=True, exist_ok=True)
        architectures.add(arch)
        for apk in sorted(artifact.rglob("*.apk")):
            destination = target / apk.name
            # One architecture builds any given package exactly once, so a
            # collision here means the spool is inconsistent - unless the bytes
            # agree, which makes the second copy harmless.
            if destination.exists() and digest(destination) != digest(apk):
                raise SystemExit(f"{apk.name}: built for {arch} twice with "
                                 "different content")
            shutil.copy2(apk, destination)
            found += 1
    return found, architectures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="spool", default=str(PROJECT / ".spool"))
    parser.add_argument("--to", dest="spooled", default=str(PROJECT / "out" / "packages"))
    args = parser.parse_args(argv)

    spool, spooled = Path(args.spool), Path(args.spooled)
    if not spool.is_dir():
        raise SystemExit("no spool directory: " + str(spool))
    stray = [str(p.relative_to(spool)) for p in spool.rglob("*.apk")
             if architecture(p.parent.name) is None]
    if stray:
        raise SystemExit("APKs outside a build artifact directory: " + ", ".join(stray))
    found, architectures = collect(spool, spooled)
    if not found:
        raise SystemExit("no packages in " + str(spool))
    print(f"collected {found} packages for {len(architectures)} architectures: "
          + " ".join(sorted(architectures)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
