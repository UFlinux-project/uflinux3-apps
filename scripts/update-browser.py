#!/usr/bin/env python3
"""Refresh aports/apps/web-browser from Alpine's community/chromium recipe.

The browser is a fork of Alpine's recipe, so "check for upstream updates" means
checking Alpine rather than Chromium: Alpine decides the version, the copium
branch, the crubit and test_fonts pins, the LLVM version and the set of musl
patches. Following it keeps the two recipes mergeable, which is the whole point
of forking one recipe instead of writing our own.

    https://raw.githubusercontent.com/alpinelinux/aports/<ver>-stable/community/chromium/

The version is only adopted once ungoogled-chromium has published a release for
it, because the recipe builds ungoogled's patch set on top of Alpine's tree.
Until then the bump is reported as `deferred` rather than `failed`: Alpine being
ahead of ungoogled by a few days is the normal case, not an error.

What is rewritten: the pins, Alpine's copium patch list, Alpine's vendored patch
files (re-fetched, so a content change is picked up too), the source list's
Alpine-owned entries, and every sha512sums entry. What is left alone: our
renamed and edited launcher and config, the ungoogled additions, and the
dependency lists.

Checksums for the large downloads are reused from Alpine rather than recomputed
- the kangie Chromium tarball is 2GB, and Alpine has already hashed the same
bytes. Anything Alpine does not list is ours and is hashed from disk or fetched.

    --check     exit 1 if the recipe is not current (no writes)

Prints `current`, `updated`, `deferred` or `failed`, in the same shape
scripts/update-checksums.py uses so .github/workflows/update.yml can turn the
`updated` lines into release notes.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "repository.json")
RECIPE = os.path.join(ROOT, "aports", "apps", "web-browser", "APKBUILD")
RECIPE_DIR = os.path.dirname(RECIPE)

APORTS = "https://raw.githubusercontent.com/alpinelinux/aports"
UNGOOGLED = ("https://github.com/ungoogled-software/ungoogled-chromium"
             "/archive/refs/tags/%s.tar.gz")

# Alpine's chromium-launcher.sh and chromium.conf are vendored under our package
# name and then edited, so re-fetching them would discard our changes; its
# google-api.keys is dropped entirely, because _configure empties the keys. Only
# .patch files are mirrored from Alpine, so these three are never fetched.
# Entries in our source list that Alpine does not have.
OURS = ["typescript_native_preview.patch.noauto", "web-browser-launcher.sh",
        "web-browser.conf"]

# Copied verbatim from Alpine. pkgrel is reset by a bump rather than compared,
# and _ugver is derived from pkgver.
PINS = ["pkgver", "_copium_tag", "_crubit", "_testfonts", "_typescript",
        "_llvmver"]

USER_AGENT = "uflinux3-apps-update-browser"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def sha512(blob):
    return hashlib.sha512(blob).hexdigest()


def block_lines(text, name):
    """Return the entries of a `name="\n\t<entry>\n\t"\n` list."""
    start = re.search(r"^" + re.escape(name) + r'="\n', text, re.M)
    if not start:
        raise RuntimeError("no " + name + "= list")
    entries = []
    for line in text[start.end():].splitlines():
        if line.strip() == '"':
            return entries
        if line.strip():
            entries.append(line.strip())
    raise RuntimeError(name + "= list is unterminated")


def replace_block(text, name, entries):
    """Rewrite the interior of a list, keeping its position and indentation."""
    start = re.search(r"^" + re.escape(name) + r'="\n', text, re.M)
    if not start:
        raise RuntimeError("no " + name + "= list")
    lines = text[start.end():].splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == '"':
            body = "".join("\t" + entry + "\n" for entry in entries)
            return (text[:start.end()] + body + "".join(lines[i:]))
    raise RuntimeError(name + "= list is unterminated")


def variables(text):
    """Return every simple `name=value` assignment above the lists.

    The recipe is never sourced here - that would need abuild's whole
    environment - so the variables in the source list are expanded by hand from
    the recipe's own text, longest name first so that $pkgver does not eat
    $pkgverx.
    """
    pairs = re.findall(r'^([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"\n]*)"|(\S+))$',
                       text, re.M)
    return {name: (quoted or bare) for name, quoted, bare in pairs}


def expand(name, values):
    for _ in range(10):
        for key in sorted(values, key=len, reverse=True):
            name = name.replace("$" + key, values[key])
        if "$" not in name:
            return name
    raise RuntimeError("cannot expand source entry " + name)


def source_names(text, values):
    """Return the on-disk name of each source entry, in order.

    abuild keys sha512sums on the file name, so the left-hand side of `::` wins
    and, for the entries written as a bare URL, the name is the last path
    component of that URL.
    """
    names = []
    for entry in block_lines(text, "source"):
        name = expand(entry.partition("::")[0], values)
        names.append(name.rsplit("/", 1)[-1])
    return names


def parse(text):
    """Read the pins, the Alpine patch set and the checksums of one recipe."""
    pins = {}
    for pin in PINS:
        match = re.search(r"^" + pin + r"=(\S+)$", text, re.M)
        if not match:
            raise RuntimeError("no " + pin + " pin")
        pins[pin] = match.group(1)
    env = variables(text)
    env.update(pins)
    sums = {}
    for line in block_lines(text, "sha512sums"):
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest
    return {
        "pins": pins,
        "env": env,
        "copium": block_lines(text, "_copium_patches"),
        "patches": [name for name in source_names(text, env)
                    if name.endswith(".patch")],
        "sums": sums,
    }


def alpine_recipe(branch):
    url = "%s/%s-stable/community/chromium/APKBUILD" % (APORTS, branch)
    return fetch(url).decode("utf-8"), url


def ungoogled_ready(ugver):
    """Hash ungoogled's release tarball, or raise if it is not out yet."""
    url = UNGOOGLED % ugver
    try:
        return sha512(fetch(url))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def rewrite(ours, alpine, ugsum):
    """Return the new recipe text, the files to write, and what moved."""
    pins = dict(alpine["pins"])
    text = ours["text"]
    delta = [pin for pin in PINS
             if ours["pins"][pin] != alpine["pins"][pin]]

    # The pins first: the source list and its checksums are spelled from them.
    for pin in PINS:
        text = re.sub(r"^" + pin + r"=\S+$", pin + "=" + pins[pin], text,
                      count=1, flags=re.M)
    text = re.sub(r"^pkgrel=\S+$", "pkgrel=0", text, count=1, flags=re.M)

    # Alpine's copium list is version-specific, so it is copied rather than
    # merged: a patch Alpine dropped has usually been upstreamed.
    if ours["copium"] != alpine["copium"]:
        text = replace_block(text, "_copium_patches", alpine["copium"])
        delta.append("_copium_patches")

    # Re-vendor every Alpine-owned patch, so a patch whose content changed at
    # the same name is refreshed too. Files are written only when they differ.
    files = {}
    for name in alpine["patches"]:
        url = "%s/%s-stable/community/chromium/%s" % (APORTS, alpine["branch"],
                                                      name)
        blob = fetch(url)
        path = os.path.join(RECIPE_DIR, name)
        try:
            with open(path, "rb") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current != blob:
            files[name] = blob
            delta.append(name)

    # The source list keeps our URL entries and our own additions verbatim; only
    # the Alpine-owned file names are reconciled, in Alpine's order.
    our_entries = block_lines(ours["text"], "source")
    our_names = source_names(ours["text"], ours["env"])
    kept = []
    dropped = []
    for entry, name in zip(our_entries, our_names):
        if name in alpine["patches"]:
            continue                    # replaced by Alpine's copy below
        if name.endswith(".patch") and name not in OURS:
            dropped.append(name)        # Alpine dropped it, usually upstreamed
            continue
        kept.append(entry)
    missing = [name for name in OURS if name not in our_names]
    if missing:
        raise RuntimeError("source list lost our own entries: " +
                           ", ".join(missing))
    text = replace_block(text, "source", kept + alpine["patches"])
    if dropped:
        delta.append("removed " + ", ".join(dropped))

    # Checksums: ours from disk (now refreshed), Alpine's for anything too big to
    # re-download, and a fresh fetch for what is left.
    env = variables(text)
    env.update(pins)
    sums = []
    for name in source_names(text, env):
        path = os.path.join(RECIPE_DIR, name)
        if name in files:
            digest = sha512(files[name])
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                digest = sha512(handle.read())
        elif name in alpine["sums"]:
            digest = alpine["sums"][name]
        elif name.startswith("ungoogled-chromium-"):
            if ugsum is None:
                raise RuntimeError("ungoogled-chromium has no release for " +
                                   pins["pkgver"] + " yet")
            digest = ugsum
        else:
            digest = sha512(fetch("%s/%s-stable/community/chromium/%s" %
                                  (APORTS, alpine["branch"], name)))
        sums.append("%s  %s" % (digest, name))
    text = replace_block(text, "sha512sums", sums)
    return text, files, delta


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the recipe is stale")
    args = parser.parse_args()

    with open(CONFIG) as handle:
        branch = json.load(handle)["alpine"]["version"]

    with open(RECIPE) as handle:
        recipe_text = handle.read()
    ours = parse(recipe_text)
    ours["text"] = recipe_text

    try:
        text, url = alpine_recipe(branch)
        alpine = parse(text)
        alpine["branch"] = branch
        alpine["url"] = url
    except (RuntimeError, OSError, urllib.error.URLError) as error:
        print("failed     web-browser: " + str(error), file=sys.stderr)
        return 1

    if ours["pins"] == alpine["pins"]:
        print("current    web-browser " + alpine["pins"]["pkgver"])
        return 0

    # Alpine moved, so the only thing that can still stop this is ungoogled.
    # Fetch its tarball now: it is both the gate and one of the checksums.
    ugsum = ungoogled_ready(alpine["pins"]["pkgver"] + "-1")
    if ugsum is None:
        print("deferred   web-browser Alpine has %s, ungoogled-chromium does "
              "not yet" % alpine["pins"]["pkgver"])
        return 0

    try:
        text, files, delta = rewrite(ours, alpine, ugsum)
    except (RuntimeError, OSError, urllib.error.URLError) as error:
        print("failed     web-browser: " + str(error), file=sys.stderr)
        return 1

    if text == ours["text"] and not files:
        print("current    web-browser " + alpine["pins"]["pkgver"])
        return 0
    print("updated    web-browser %s -> %s (%s)" %
          (ours["pins"]["pkgver"], alpine["pins"]["pkgver"], ", ".join(delta)))
    if args.check:
        return 1

    # Nothing is written until every input resolved, so a failure part-way
    # through cannot leave the recipe at one version and its patches at another.
    for name, blob in files.items():
        with open(os.path.join(RECIPE_DIR, name), "wb") as handle:
            handle.write(blob)
    fd, tmp = tempfile.mkstemp(dir=RECIPE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, RECIPE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("wrote      " + os.path.relpath(RECIPE, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
