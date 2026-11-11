#!/usr/bin/env python3
"""Generate aports/apps/<app>/APKBUILD for every Craft app.

The 15 recipes differ only in version, checksum, description and a handful of
native dependencies, so they are generated from one table plus config/apps.lock.json
(which scripts/update-checksums.py refreshes from the GitHub API). A version bump is
therefore: update the lockfile, run this script, commit the diff.

    python3 scripts/gen-app-recipes.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "config", "apps.lock.json")
APKS = os.path.join(ROOT, "aports", "apps")

# Alpine dev packages, keyed by the -sys crate that pulls them in. Verified against
# the aports tree for 3.24: alsa-lib/glib/mesa/libdrm/fontconfig are in main,
# gtk+3.0/pipewire/aws-lc/desktop-file-utils/appstream are in community.
DEP_ALSA = "alsa-lib-dev"
DEP_GTK = "gtk+3.0-dev glib-dev"
DEP_FONTCFG = "fontconfig-dev"
DEP_PW = "pipewire-dev mesa-dev libdrm-dev"

# Per-app deltas. Everything else is uniform and lives in craft-common.sh.
#   desc    -> pkgdesc (upstream's own desktop Comment, capitalised as a phrase)
#   bins    -> binaries to build and install; defaults to [app, app-cli]
#   feats   -> cargo features, qualified the way upstream's package.sh passes them
#   man     -> upstream ships a man-and-completions example for the CLI
#   mk      -> extra makedepends beyond the common set
#
# The common set needs explaining: git because every app depends on git repos
# (craft-libs, egui, winit) with no crates.io tarball; cmake + samurai + perl
# because ring and aws-lc-sys compile bundled C/asm through a CMake or cc-rs
# build; desktop-file-utils + appstream because craft-common.sh runs upstream's
# own metadata validators.
APPS = {
    "anicraft": {
        "desc": "Draw and animate 2D vector art on a timeline",
        "mk": DEP_ALSA,
    },
    "cadcraft": {
        "desc": "Draft, dimension and plot 2D drawings; open DXF and DWG",
        "man": True,
    },
    "deckcraft": {
        "desc": "Create and present slide decks",
        "mk": DEP_ALSA,
    },
    "designcraft": {
        "desc": "Lay out magazines, books and print documents",
    },
    "effectcraft": {
        "desc": "Motion graphics and visual effects compositor",
        "mk": DEP_ALSA,
    },
    "figcraft": {
        "desc": "Design and prototype user interfaces",
        "bins": ["figcraft", "figcraft-cli"],
        "mk": DEP_FONTCFG,
    },
    "filmcraft": {
        "desc": "Edit video, color and sound",
        # muda's native menu bar uses GTK here, unlike the other apps.
        "mk": DEP_ALSA + " " + DEP_GTK,
    },
    "lightcraft": {
        "desc": "Organise photos and develop raw files, non-destructively",
        "feats": ["lightcraft/heif", "lightcraft-cli/heif"],
    },
    "materialcraft": {
        "desc": "Design procedural PBR materials with node graphs",
    },
    "pdfcraft": {
        "desc": "Read, organize, combine, split and secure PDFs",
    },
    "photocraft": {
        "desc": "Edit photos and layered PSD documents",
        "feats": ["heif"],
        "man": True,
        "mk": " ".join(sorted([DEP_FONTCFG, DEP_PW])),
    },
    "soundcraft": {
        "desc": "Record, edit and mix audio and MIDI",
        "man": True,
        "mk": DEP_ALSA,
    },
    "texturecraft": {
        "desc": "Paint PBR textures directly on 3D meshes",
    },
    "vectorcraft": {
        "desc": "Draw and edit vector illustrations, SVG and PDF",
    },
    "wordcraft": {
        "desc": "Write and design documents; open and save Word files",
        "mk": DEP_FONTCFG,
    },
}

TEMPLATE = """\
# Maintainer: uflinux3-apps contributors <noreply@uflinux.org>
pkgname={pkgname}
pkgver={pkgver}
pkgrel=0
pkgdesc="{desc}"
url="https://getartcraft.com/apps/{app}"
arch="all"
license="Apache-2.0"
# Upstream resolves git dependencies (craft-libs, egui, winit forks) that have no
# crates.io tarballs, so the build cannot be offline.
options="net !check"
makedepends="
\tcargo
\trust
\tgit
\tcmake
\tsamurai
\tperl
\tdesktop-file-utils
\tappstream
{mk}
\t"
depends="
\twayland-libs-egl
\thicolor-icon-theme
\tshared-mime-info
{dep}
\t"
subpackages="
\t$pkgname-doc
{sub}
\t"
source="{pkgname}-{pkgver}.tar.gz::{url}"
builddir="$srcdir/{app}-{pkgver}"
{vars}
sha512sums="
{sum}  {pkgname}-{pkgver}.tar.gz
"
"""


def indent_list(items, pad="\t"):
    return "\n".join(pad + i for i in items)


def render(app, lock, meta):
    bins = meta.get("bins") or [app, app + "-cli"]
    feats = meta.get("feats") or lock.get("features") or []
    man = bool(meta.get("man") or lock.get("man_completions"))

    mk = ["\t" + d for d in (meta.get("mk") or "").split() if d]
    # Anything actually linked (libwayland-egl, libGL, libEGL, libasound, ...) is
    # filled in by abuild's tracedeps, so only non-obvious runtime requirements are
    # listed here explicitly.
    dep = []
    extra = meta.get("mk") or ""
    if DEP_ALSA in extra:
        dep.append("\talsa-lib")
    if DEP_GTK in extra:
        dep.append("\tgtk+3.0")
    if DEP_FONTCFG in extra:
        # fontconfig has no separate -libs subpackage: the library lives in the
        # main package.
        dep.append("\tfontconfig")
    if "pipewire-dev" in extra:
        dep += ["\tpipewire-libs", "\tmesa-gbm", "\tlibdrm"]

    sub = []
    if man:
        sub = [
            "\t$pkgname-bash-completion",
            "\t$pkgname-zsh-completion",
            "\t$pkgname-fish-completion",
        ]

    vars_ = "_craft_app=\"%s\"\n_craft_bins=\"%s\"" % (app, " ".join(bins))
    if feats:
        vars_ += "\n_craft_features=\"%s\"" % " ".join(feats)
    if man:
        vars_ += "\n_craft_man=yes"
    vars_ += "\n\n. \"$startdir/../craft-common.sh\""

    out = TEMPLATE.format(
        pkgname=app,
        pkgver=lock["version"],
        desc=meta["desc"],
        app=app,
        mk="\n".join(mk),
        dep=indent_list(dep, ""),
        sub="\n".join(sub),
        url=lock["url"],
        vars=vars_,
        sum=lock["sha512"],
    )
    # An unused optional block expands to a blank line; the recipes have no other
    # blank lines, so dropping them keeps the lists tight.
    return "".join(line + "\n" for line in out.splitlines() if line.strip())


def main():
    with open(LOCK) as f:
        locked = json.load(f)["apps"]

    missing = set(locked) ^ set(APPS)
    if missing:
        sys.exit("apps.lock.json and this script disagree: " + ", ".join(sorted(missing)))

    for app in sorted(APPS):
        out = os.path.join(APKS, app, "APKBUILD")
        new = render(app, locked[app], APPS[app])
        old = None
        if os.path.exists(out):
            with open(out) as f:
                old = f.read()
        if old == new:
            print("unchanged  " + app)
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(new)
        print("wrote      " + app)


if __name__ == "__main__":
    main()
