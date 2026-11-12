# Adding or updating a package

## The fifteen Craft apps

Their recipes are generated. `scripts/gen-app-recipes.py` holds the per-app
deltas (description, extra `-dev` packages, optional cargo features, whether
upstream ships a man page) and `config/apps.lock.json` holds the version, the
tarball hash and the binary names. The common phases live in
`aports/apps/craft-common.sh`, which each recipe sources, so a change to how
Craft apps are built is a one-line change for all fifteen.

### Bumping a version

Usually nothing needs to happen: `.github/workflows/update.yml` runs the script
below once a day and releases the result. Bump by hand to pin an app to an older
version than the newest release, to unpin one, or to get a fix out without waiting
for the next scheduled run.

```sh
python3 scripts/update-checksums.py            # all apps
python3 scripts/update-checksums.py --app photocraft
```

The script asks GitHub for the newest non-prerelease release of each app,
downloads the tarball, reads `[workspace.package] version` from its root
`Cargo.toml` and refuses to record a tag whose version does not match — that
cross-check is what keeps a mis-tagged release from producing a package whose
`pkgver` lies. It then rewrites the lockfile and runs the recipe generator, so
the commit it leaves behind is exactly what CI expects. It writes nothing unless
every app resolved: one unreachable repository leaves the lockfile alone and exits
nonzero, which is why the update workflow reports it instead of releasing a bump
that quietly omits an app.

`python3 scripts/update-checksums.py --check` does the same comparison without
writing anything. The build workflow runs it on every run and turns a stale
lockfile into a warning annotation rather than a failure: a release published
yesterday is not a defect in the change being built, and it should not hold up
publishing the other fourteen packages. Now that the update workflow releases
daily, that annotation is a backstop - it goes off if the update workflow is
broken or disabled, not in the normal course of events. A checksum that is simply
*wrong* is a different matter - abuild verifies it and fails the build.

`web-browser` is not in the lockfile and is not touched by any of this. It is
still bumped automatically, but by `scripts/update-browser.py` in the same daily
run, following Alpine rather than a release feed; see
[docs/browser.md](browser.md).

### Adding an app

1. Add it to `APPS` in `scripts/gen-app-recipes.py`: the description, and any of
   `bins` (defaults to `<app>` and `<app>-cli`), `feats`, `man` and `mk` — the
   extra `makedepends`, using the `DEP_*` names already defined there.
2. Run the generator. It fetches nothing and only writes the recipe:

   ```sh
   python3 scripts/gen-app-recipes.py
   ```

3. Add the app to `config/apps.lock.json` by running the update script for it.
   The generator cross-checks the app set against the lockfile and fails if the
   two disagree, so an app missing from either side is caught immediately.
4. Build it once locally (`sudo scripts/build.sh --arch x86_64 --package <app>`)
   before opening the pull request. `abuild` will tell you about a missing
   `makedepend` or an unexpected file left in the recipe directory.

If the new app needs a shared library that Alpine packages, add the `-dev`
package to `mk` and the runtime package to the recipe's `depends`: the point of
this repository is that an installed app reuses the libraries the base system
already has.

### UI fonts and translations

Every Craft build logs one warning that is expected:

```
warning: UI fonts: no craft-fonts found (set CRAFT_FONTS_DIR or clone
storytold/craft-fonts next to the repo); using the built-in fallback fonts
```

Each app's `build.rs` looks for two optional build inputs that upstream's release
workflow supplies from `craft-common-actions`: a `craft-fonts` checkout
(`CRAFT_FONTS_DIR`, `CRAFT_FONTS_REQUIRED=1`), whose Inter and JetBrains Mono
replace egui's bundled faces, and a `craft-i18n` checkout (`CRAFT_I18N_DIR`),
which turns the interface catalogs in. We build without either, so our packages
use the fallback fonts and are English-only.

This is a considered trade, not an oversight. `craft-fonts` has no releases, so
it can only be pinned by commit, and the pin differs per app: the fifteen apps
presently name five different revisions, each a 29-39 MB tarball, which would
have to be vendored, hashed and bumped separately for every application. Worse,
the pins are not self-consistent - several of them predate the Latin catalog
entirely and contain no `Inter-Regular.ttf` at all, so honoring them literally
would still leave the warning standing. `craft-i18n` is pinned by a
`craft-i18n.rev` file that only `pdfcraft` and `wordcraft` currently have.

If that stops being the right answer, the mechanism is contained: put a
`.noauto` tarball in each recipe's `$source`, unpack it to
`aports/apps/craft-fonts` - which every app's `discover()` finds, since it walks
up four directories from the manifest - and export `CRAFT_FONTS_DIR` in
`craft-common.sh`. `update-checksums.py` would then have to learn to read
`craft-fonts-ref` out of each app's `release.yml`, so the lockfile tracks
upstream's choice rather than ours.

## A package of its own

`web-browser` is the non-generated case: a recipe that is a real fork of an
Alpine package, with its own patches and data files next to it. The rules that
apply to any recipe here:

- One directory per package under `aports/apps/`, and nothing in it that is not
  listed in `$source`. abuild only warns about a stray file, so the warning is
  easy to miss in a long log; keeping the directory clean is what makes it
  meaningful when it does appear. A leftover `src/` or `pkg/` from a local build
  is fine — those are on abuild's allow-list.
- `sha512sums` is positional and matches `$source` entry by entry. The name
  abuild hashes is the left-hand side of `::`, so renaming a `source` entry
  means regenerating the checksums (`abuild -F checksum`).
- Anything that must be fetched and checksummed but not unpacked, or unpacked
  but not auto-applied, uses the `.noauto` suffix — the same convention Alpine
  uses. See [browser.md](browser.md) for why that matters.
- `arch="all"` unless the package genuinely cannot work somewhere. The build
  matrix in `scripts/ci-matrix.py` decides what is actually built for which
  architecture, and `config/repository.json` is where that is configured; a
  recipe's own `arch=` is the second line of defence, which is why
  `web-browser` repeats `browser_arches` as `arch="aarch64 x86_64"`.
- Recipe shell code has to parse under **busybox ash**, because that is the
  interpreter abuild runs (`#!/bin/ash -e`). Bashisms are the nasty class of bug
  here: `${var^^}` or `${var:0:1}` pass `sh -n` on a Debian/Ubuntu runner and then
  abort the build halfway through, reported as a syntax error inside `abuild`
  itself. CI checks every shell file in the repository — `find aports scripts`
  restricted to `*.sh` and `APKBUILD`, pruning `src`/`pkg`/`tmp` — which is
  deliberately a walk and not a glob, because `aports/apps/*/*.sh` misses
  `aports/apps/craft-common.sh`, the helper every generated recipe sources.
  Locally:
  `podman run --rm --privileged -v "$PWD":/p:ro alpine busybox ash -n /p/aports/...`
  (`--privileged` because SELinux otherwise denies the container read access to a
  home directory bind mount).

## How CI decides what to build

`scripts/ci-matrix.py` reads the three config files and emits the build matrix
plus the outputs the publish job needs:

- a tag `v*`, a manual run or the weekly schedule builds every package for every
  architecture and publishes the result. Tags are dates
  (`v<year>.<month>.<number-in-month>`) and are normally created by the update
  workflow, which pushes one whenever upstream moves and then dispatches this
  workflow for it - a tag pushed with `GITHUB_TOKEN` would not start a run on its
  own, since GitHub ignores workflow events triggered by that token;
- anything else - a pull request, or a push to the branch the workflow watches -
  builds `x86_64` only and does not publish;
- `web-browser` appears only on the architectures in `browser_arches`;
- `full` says which of those two cases this is, and `publish` gates on it rather
  than keeping its own copy of the rule - publishing replaces the entire
  repository, so a run that built one architecture must not deploy one
  architecture and silently drop the rest;
- `cache` is a digest of the configs plus every recipe body before its
  `sha512sums=` line, so bumping a version reuses the (expensive) dependency
  cache while changing a dependency list invalidates it;
- `epoch` is the commit timestamp, used as `SOURCE_DATE_EPOCH` so the index is
  reproducible.

A run that would publish is also required to be able to sign: `checks` fails
immediately when `full` is set and the signing secrets are missing, which is what
happens to a tag pushed to a fork. See the next section for why the *build* job
needs the key too, not only the one that publishes.

Emulation is what makes the matrix expensive, and `build.py` handles it rather
than the workflow: it compares the target against the runner's own `uname -m` and,
when qemu-user is involved, sets `CARGO_BUILD_JOBS=1` so instruction translation
stays inside the runner's memory. It also disables LTO there. That second part is
specific to the Craft apps, whose release profile asks for thin LTO with a single
codegen unit, which makes the final link of the biggest binary one
single-threaded pass over the whole program - measured at over half an hour on
native x86_64, and the phase most likely to run past a runner's time limit under
emulation, being the one phase more jobs cannot shorten. Turning LTO off makes
that link parallel and much shorter for a slightly larger binary, a trade worth
making only where the alternative is that the architecture is not published at
all; a 64-bit native build gets upstream's own profile untouched. The LTO part of
the override is keyed on the *target* being 32-bit (`NARROW` in `build.py`), not
on qemu: a 32-bit `rustc` is a 32-bit process with barely 3 GiB of address space
whatever runs it, and the same single-codegen-unit link fails with `rustc-LLVM
ERROR: out of memory` on an x86_64 kernel as surely as under emulation - which is
why the `x86` shards of the bigger apps used to die while the 64-bit ones passed.
(The browser reaches the same ceiling from the other direction on 32-bit, where
ThinLTO is what makes the link fit in memory at all; see the LTO section of
[docs/browser.md](browser.md).)

The three apps that ship a man page (`cadcraft`, `photocraft`, `soundcraft`) build
their documentation generator as a second `cargo` run, which is upstream's own
arrangement and costs a large part of the graph a second compile - see the comment
in `craft-common.sh`. That is the main reason those three shards are the slow ones.

Every shard uploads its own artifact, and `collect-packages.py` keeps the
architectures strictly apart, because the same package name is expected to appear
in several of them: `anicraft-0.1.0-r0.apk` is built once per architecture, and
abuild builds `-doc` and the completion subpackages as `subpkgarch=noarch`, which
the build stage files into the *building* architecture's directory anyway. Each
architecture's directory is therefore self-contained, and `apk index
--rewrite-arch` gives those subpackages the real architecture, so a client needs
only the one index. That is deliberate: a separate `noarch/` directory would work
too, but it would make the landing page and the "Indexed for" list depend on
whether a given package happened to have a subpackage.

## Required repository configuration

GitHub Pages has to be enabled for the repository, with the workflow as the
source. The workflow needs two secrets, both copied from the UFLinux repository,
so this repository signs with the key UFLinux already trusts:

- `APK_SIGNING_PRIVATE_KEY` — the contents of `signing/uflinux3.ec384`
- `APK_SIGNING_PUBLIC_KEY` — the contents of `signing/uflinux3.ec384.pub`

An ECDSA keypair over P-384, not RSA: `openssl ecparam -name secp384r1 -genkey
-noout` for the private half, `openssl pkey -pubout` for the public one.
`abuild-keygen` cannot make this key, since it only ever runs `openssl genrsa`.
The signature member is still named `.SIGN.RSA256.` whatever the key type, because
apk-tools derives the required digest from that name and accepts only SHA-256.

Both jobs need them, not just the one that publishes. abuild signs every package
it builds and `apk index` verifies each package's own signature before it will
describe it, so a build shard that cannot see the key signs with a throwaway of
its own and the index then rejects its output as `BAD signature`. Since the index
replaces the whole repository, a run that cannot sign must not publish either:
`checks` fails such a run up front rather than after every package has been
built, and without the secrets the index stage would otherwise generate a
throwaway key that no existing client can verify.
