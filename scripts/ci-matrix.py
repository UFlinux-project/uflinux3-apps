#!/usr/bin/env python3
"""Decide what the CI workflow builds and key its caches.

Writes GitHub Actions outputs:

  packages  one entry per (architecture, package) build shard
  full      "true" when every architecture was built, i.e. whether the result
            may replace the published repository
  release   "true" when the event is a version tag
  epoch     SOURCE_DATE_EPOCH for reproducible APK and APKINDEX timestamps
  cache     digest of everything that changes dependency resolution
  repository  the path under the Pages root that the repository is served from

A tag, a manual run or the weekly schedule builds every architecture. Anything
else - a pull request, or a push to the branch the workflow watches - builds
x86_64 only, which keeps the common change cheap while still exercising the full
recipe and index pipeline. `full` says which case a run is, and `publish` gates
on it rather than keeping its own copy of the rule: publishing replaces the
entire repository, so a run that built one architecture must not deploy one
architecture and silently drop the rest.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The browser is not a Craft app and is not version-locked in apps.lock.json.
BROWSER = "web-browser"


def package_names(apps):
    return sorted([*apps, BROWSER])


def configuration(arches, repository, apps, event, ref):
    release = event == "push" and ref.startswith("refs/tags/v")
    if release and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", ref[len("refs/tags/"):]):
        raise ValueError("Tag must be v followed by a numeric version, e.g. v3.24.1")
    if event not in {"push", "pull_request", "workflow_dispatch", "merge_group", "schedule"}:
        raise ValueError("Unsupported event " + repr(event))
    # Only a run that is meant to be published is worth building seven times
    # over; a push to the watched branch is a cheap x86_64 smoke test.
    full = release or event in {"workflow_dispatch", "schedule"}
    selected = list(arches) if full else ["x86_64"]
    unknown = [arch for arch in selected if arch not in arches]
    if unknown:
        raise ValueError("architectures.json is missing " + " ".join(unknown))
    browser = set(repository["browser_arches"])
    return {
        # The index job needs no matrix of its own: it indexes whatever
        # architectures collect-packages.py found in the artifact spool.
        "packages": {"include": [
            {
                "arch": a,
                "runner": arches[a]["runner"],
                "package": p,
                # Decides one thing: whether the shard installs qemu-user. This
                # list of "needs no emulation" architectures is deliberately
                # narrower than build.py's own NATIVE table, which also counts
                # x86 on an x86_64 runner and armv7 on an aarch64 one. Installing
                # qemu where the kernel would not have needed it is harmless - the
                # native ELF handler has higher binfmt priority and wins - while
                # the opposite would leave the shard without a translator. The
                # reason the job's timeout is so long.
                "emulated": "true" if a not in {"x86_64", "aarch64"} else "false",
            }
            for a in selected
            for p in package_names(apps)
            if p != BROWSER or a in browser
        ]},
        "release": str(release).lower(),
        "full": str(full).lower(),
    }


def dependency_digest(arches, repository):
    # Everything that can change which dependencies are installed, or which
    # bytes a recipe feeds to the compiler, but not the source checksums:
    # bumping a version must still reuse the (expensive) dependency cache.
    digest = hashlib.sha256()
    digest.update(json.dumps({"arches": arches, "repository": repository}, sort_keys=True).encode())
    for recipe in sorted((ROOT / "aports/apps").glob("*/APKBUILD")):
        digest.update(recipe.read_bytes().split(b"sha512sums=")[0])
    digest.update((ROOT / "aports/apps/craft-common.sh").read_bytes())
    return digest.hexdigest()


def main():
    arches = json.loads((ROOT / "config/architectures.json").read_text())
    repository = json.loads((ROOT / "config/repository.json").read_text())
    apps = json.loads((ROOT / "config/apps.lock.json").read_text())["apps"]
    epoch = subprocess.check_output(["git", "log", "-1", "--format=%ct"], text=True).strip()
    outputs = {
        **configuration(arches, repository, apps, os.environ.get("EVENT", ""), os.environ.get("REF", "")),
        "epoch": epoch,
        "cache": dependency_digest(arches, repository),
        # The publish job's summary quotes the path the repository is served
        # under; emitting it here keeps the workflow from hardcoding a third
        # copy of a layout rule that config/repository.json owns.
        "repository": repository["repository_path"],
    }
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={json.dumps(value) if isinstance(value, dict) else value}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as failure:
        # The workflow's `on.push.tags: v*` accepts a tag this script rejects, and
        # a traceback buries the reason. Annotate it instead.
        print("::error::ci-matrix.py: " + str(failure), file=sys.stderr)
        sys.exit(1)
