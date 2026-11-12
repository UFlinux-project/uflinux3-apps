# The `web-browser` package

`aports/apps/web-browser/APKBUILD` is a fork of Alpine's `community/chromium`
recipe for the branch named in `config/repository.json`, rebuilt on top of
[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)
and shipped under a different name so that it can be installed next to Alpine's
own `chromium`.

Alpine's work is kept verbatim wherever possible: the same kangie source tarball
(`chromium-$pkgver-linux.tar.xz`, already stripped of the prebuilt blobs Alpine
does not want), the same unbundled system libraries, the same *copium* Rust
import graph, the same LLVM/GN/Rust/TypeScript toolchain, and all of Alpine's
musl patches. Every place where the recipe deviates is marked `## NOTE` in the
APKBUILD. This file is the long form of those notes.

## Why it looks like this

Three upstreams have to agree on one source tree:

1. **Alpine** provides the packaging: the tarball, the 20 musl/system-library
   patches, the `gn` flags, the subpackage layout, and the `copium` patches that
   graft a distribution Rust toolchain onto Chromium's vendored one.
2. **ungoogled-chromium** provides the de-Googling: a list of pruned binaries, a
   patch series, and a domain-substitution step that rewrites Google host names
   into a placeholder.
3. **This repository** provides the identity: `pkgname=web-browser`, the
   `Web Browser` menu label, and Papirus's `web-browser` icon instead of
   Chromium's logo.

`prepare()` runs them in that order — prune, patch, substitute, then
`default_prepare` — because Alpine's and copium's patches were written against a
tree that ungoogled has already rewritten, and none of the files they touch is a
file ungoogled prunes, patches or substitutes. If that ever stops being true, the
failure mode is a rejected hunk in `prepare()`, not a broken browser.

The deviations worth knowing about:

- **`Icon=web-browser`.** Papirus ships that name as a full-colour SVG in every
  fixed size plus a symbolic variant, so the desktop file refers to it by name
  and the hicolor PNGs from upstream are installed only as a fallback for themes
  that do not know the name. `papirus-icon-theme` is therefore a runtime
  dependency, not a build one.
- **No `chromium` or `chromium-browser` symlink, no `provides`.** Alpine installs
  those names because `xdg-utils` looks `chromium-browser` up; we skip them so the
  two packages can coexist. Everything points at `web-browser`.
- **Our own launcher.** `web-browser-launcher.sh` is Alpine's
  `chromium-launcher.sh` with the names changed: `/usr/bin/web-browser` symlinks
  to it, it lives in `/usr/lib/web-browser` next to the binary (which is still
  called `chromium`), and it exports `CHROME_DESKTOP=web-browser.desktop` so the
  running browser finds its own menu entry. `web-browser.conf` is the system
  flag file it sources from `/etc/web-browser`.
- **Empty Google API keys.** Alpine ships a base64 blob of its own keys. To keep
  the package self-contained and the diff small, all three stay empty and
  `use_official_google_api_keys=false`. To enable sync, create keys at
  <https://www.chromium.org/developers/how-tos/api-keys> and fill in
  `google_api_key`, `google_default_client_id` and
  `google_default_client_secret` in `_configure`.
- **`!check`.** Domain substitution rewrites exactly the host names the `net/`
  and `safe_browsing/` tests assert on, and ungoogled's patches drop the
  test-only targets Alpine's `check()` would run.
- **MEIPreload is conditional.** ungoogled's `disable-mei-preload.patch` removes
  the data dependency, so the component is never built and the directory does not
  exist.
- **Vendored TypeScript patch.** ungoogled prunes
  `third_party/typescript/linux-amd64` as a prebuilt blob, so `_prepare()`
  recreates it from Alpine's `typescript-go` package and applies our own copy of
  Alpine's 3pp patch. It is vendored as `typescript_native_preview.patch.noauto`:
  the `.noauto` suffix keeps abuild's `default_prepare()` from applying it a
  second time against `$builddir`, where `lib/lib.dom.d.ts` does not exist. The
  same trick is what Alpine itself uses for the `typescript-*.tgz.noauto` tarball.

## Architecture support

`arch="aarch64 x86_64"`, matching `browser_arches` in `config/repository.json`.
Two of Alpine's build dependencies for this recipe — `chromium-rust-src` and
`typescript-go` — only exist for `aarch64`, `armv7` and `x86_64` in the current
Alpine release, and `armv7` runs out of memory while linking on Alpine's own
builders. Adding an architecture therefore means adding a toolchain, not a
patch. `scripts/build.py` skips the package with a note instead of failing when
CI asks for an unsupported architecture.

### LTO

`_configure` sets `use_thin_lto` from a `case "$CARCH"` rather than copying
Alpine's flat `use_thin_lto=false`:

- **64-bit** (`x86_64`, `aarch64`, and the other 64-bit arches Alpine has) keeps
  Alpine's setting. There the final link is already the peak-memory phase, and
  LTO in any form only makes it worse.
- **32-bit** opts in to ThinLTO. A non-LTO Chromium's single whole-program link
  is what OOMs an armv7 builder; ThinLTO splits that into per-module parallel
  passes, which Chromium's own docs put at 3x-10x faster and far lighter than
  full LTO. Full LTO is not an option at all — Chromium 156's `compiler.gni`
  exposes only the `use_thin_lto` boolean, and `-flto=full` appears in the build
  solely under `is_wasm`.

abuild itself adds no LTO flags, so GN is the only knob here. (The Craft apps'
`CARGO_PROFILE_RELEASE_LTO=false` in `scripts/build.py` is a separate,
Cargo-only matter.)

`armv7` is still not in `arch=`, because of the caveat marked `## NOTE` in
`_configure`: enabling LTO makes Chromium's build `assert(use_lld)`, and
`use_lld` is forced off by the recipe's `use_mold=true`. Before armv7 is enabled,
a 32-bit build has to drop mold or pass `use_mold=false` alongside
`use_thin_lto=true`. The flag is conditional now so that decision is a one-line
change rather than a re-derivation.

## Bumping the version

`scripts/update-browser.py` does the mechanical part of a bump, and
`.github/workflows/update.yml` runs it daily alongside the Craft app checks. It
reads `alpine.version` from `config/repository.json`, fetches Alpine's
`community/chromium/APKBUILD` for that branch, and — only once ungoogled-chromium
has published a release for the new version — rewrites the pins below, resets
`pkgrel`, copies Alpine's `_copium_patches` list, re-vendors Alpine's `*.patch`
files and its `source` entries, and regenerates `sha512sums`. Checksums for the
large downloads are reused from Alpine rather than recomputed; the kangie
tarball is 2GB.

Until ungoogled catches up the bump is reported as `deferred` rather than
`failed`, and the next run picks it up. Alpine being a few days ahead is the
normal case.

What stays manual:

- **Dependency lists.** `makedepends` changes are read, not written: a new
  unbundled library needs a `gn` flag too, and that is a judgement call.
- **`_llvmver`.** It is copied from Alpine, but it has to exist in the Alpine
  release we target; if it does not, the bump has to wait for the toolchain.
- **Our own files.** `web-browser-launcher.sh` and `web-browser.conf` are
  Alpine's `chromium-launcher.sh` and `chromium.conf` renamed and edited, so the
  script never re-fetches them; check those two files' diffs against Alpine by
  hand when a release changes them. `google-api.keys` is dropped entirely.
- **The build.** Nothing here replaces building the recipe in a chroot before
  pushing; the script guarantees the recipe is consistent, not that Chromium
  compiles.

For reference, the pins a bump moves and where each one comes from:

1. `pkgver` — the newest stable release. Alpine's `community/chromium` version is
   the safest choice, because the whole recipe is derived from that revision.
2. `_copium_tag` — the copium branch matching the new major version.
3. `_ugver` — the ungoogled release for that exact version, `"$pkgver-1"` unless
   upstream has had to re-release.
4. `_crubit` — `CRUBIT_REVISION` in Chromium's `tools/rust/update_rust.py` at the
   new tag.
5. `_testfonts` — the `object_name` for the new version in Chromium's `DEPS`
   file.
6. `_typescript` — the version Alpine's `typescript-go` package currently ships.
7. `_llvmver` — the LLVM major version Alpine's own recipe uses for that branch.
   It has to exist in the Alpine release we target; if it does not, the bump has
   to wait for the toolchain.

The `sha512sums` list is positional: abuild matches it against `$source` entry by
entry, and the name it hashes is the left-hand side of `::`, so an entry that is
out of order or renamed fails loudly rather than silently. To regenerate it by
hand instead of with the script:

```sh
# From a checkout, inside an Alpine chroot with coreutils and curl:
abuild -F checksum            # fetches everything and prints the new block
```

`abuild` also warns about any file in the recipe directory that is not listed in
`$source`, so leftover patches from the previous version show up here too. Run
`scripts/update-browser.py --check` to confirm a hand-made bump matches Alpine.
