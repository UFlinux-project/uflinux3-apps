#!/usr/bin/env python3
"""Refresh config/apps.lock.json from GitHub and regenerate the recipes.

For every Craft app this resolves the newest non-prerelease upstream release,
downloads the source tarball, hashes it and stores the result. The lockfile is
the only place a version number lives; scripts/gen-app-recipes.py turns it into
the 15 APKBUILDs, so a bump is:

    sudo -u builder python3 scripts/update-checksums.py   # or --check in CI

and then commit the diff.

The GitHub REST API is deliberately not used: unauthenticated requests are
limited to 60/hour per IP and shared runners routinely exceed that, while the
HTML endpoints below are not rate limited. "Latest stable" comes from following
the /releases/latest redirect, which GitHub resolves to the newest non-draft,
non-prerelease release.

    --check     exit 1 if the lockfile is not current (no writes)
    --app APP   only consider APP (repeatable)
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "config", "apps.lock.json")
GENERATOR = os.path.join(ROOT, "scripts", "gen-app-recipes.py")

# Written in this order so the file is stable across runs. Anything missing from
# an existing entry is dropped, anything new is an error rather than a surprise.
KEYS = ["repo", "tag", "version", "url", "sha512", "bins", "features",
        "man_completions"]

# A stable release tag: vMAJOR.MINOR.PATCH with no pre-release or build suffix.
STABLE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

USER_AGENT = "uflinux3-apps-update-checksums"


def head(url):
    """Return the final URL after redirects, without downloading the body."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT},
                                     method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.geturl()


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def latest_tag(repo):
    """Resolve the newest stable release tag of a GitHub repository."""
    target = head("https://github.com/" + repo + "/releases/latest")
    match = re.match(r"^https://github\.com/[^/]+/[^/]+/releases/tag/([^/]+)$",
                     target)
    if not match:
        raise RuntimeError(repo + ": no releases at all? " + target)
    tag = urllib.parse.unquote(match.group(1))
    if not STABLE.match(tag):
        raise RuntimeError(repo + ": newest release " + tag + " is not stable")
    return tag


def workspace_version(blob, url):
    """Read [workspace.package] version from the tarball's root Cargo.toml.

    craft-common.sh repeats this check at build time; doing it here means a
    mismatched tag fails while updating, not six hours into a build.
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        names = archive.getnames()
        roots = {n.split("/")[0] for n in names if "/" in n}
        if len(roots) != 1:
            raise RuntimeError(url + ": not a single-directory tarball")
        root = roots.pop()
        if root + "/Cargo.toml" not in names:
            raise RuntimeError(url + ": no Cargo.toml at the root")
        text = archive.extractfile(root + "/Cargo.toml").read().decode("utf-8")
    in_pkg = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_pkg = line == "[workspace.package]"
        elif in_pkg and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(url + ": no [workspace.package] version")


def update(app, entry):
    repo = entry["repo"]
    tag = latest_tag(repo)
    url = ("https://github.com/" + repo + "/archive/refs/tags/" + tag +
           ".tar.gz")
    blob = fetch(url)
    sha512 = hashlib.sha512(blob).hexdigest()
    version = tag[1:]
    found = workspace_version(blob, url)
    if found != version:
        raise RuntimeError(app + ": tag says " + version + " but Cargo.toml says "
                           + found)
    new = {
        "repo": repo,
        "tag": tag,
        "version": version,
        "url": url,
        "sha512": sha512,
        "bins": entry["bins"],
        "features": entry["features"],
        "man_completions": entry["man_completions"],
    }
    if set(new) != set(entry) or set(new) != set(KEYS):
        raise RuntimeError(app + ": unexpected keys " + ", ".join(sorted(entry)))
    return new


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the lockfile is stale")
    parser.add_argument("--app", action="append", dest="apps",
                        help="only consider this app (repeatable)")
    args = parser.parse_args()

    with open(LOCK) as handle:
        lock = json.load(handle)
    apps = lock["apps"]

    if args.apps:
        unknown = set(args.apps) - set(apps)
        if unknown:
            sys.exit("unknown app: " + ", ".join(sorted(unknown)))
        selected = args.apps
    else:
        selected = sorted(apps)

    changed = []
    failed = []
    for app in selected:
        try:
            new = update(app, apps[app])
        except (RuntimeError, OSError, urllib.error.URLError) as error:
            failed.append(app)
            print("failed     " + app + ": " + str(error), file=sys.stderr)
            continue
        old = apps[app]
        delta = [k for k in KEYS if old.get(k) != new[k]]
        if not delta:
            print("current    " + app + " " + new["version"])
            continue
        print("updated    %s %s -> %s (%s)" % (app, old["version"], new["version"],
                                               ", ".join(delta)))
        apps[app] = new
        changed.append(app)

    if failed:
        return 1
    if args.check:
        if changed:
            print("lockfile is stale: " + ", ".join(changed), file=sys.stderr)
        return 1 if changed else 0
    if not changed:
        return 0

    lock["apps"] = {name: {k: apps[name][k] for k in KEYS}
                    for name in sorted(apps)}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(LOCK), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(lock, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, LOCK)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("wrote      " + os.path.relpath(LOCK, ROOT))

    # The recipes are a pure function of the lockfile, so regenerate them here
    # rather than leaving that as a step someone forgets.
    return subprocess.call([sys.executable, GENERATOR])


if __name__ == "__main__":
    sys.exit(main())
