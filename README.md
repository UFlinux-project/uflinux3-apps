# uflinux3-apps

Third-party applications for [UFLinux](https://github.com/UFlinux-project/uflinux3),
packaged as Alpine APKs and published as an APK repository on GitHub Pages.

Everything is built the way Alpine's own builders build it: an Alpine minirootfs
is unpacked into a directory, `abuild` runs inside that chroot, and the resulting
`.apk` files are indexed with `apk index` and signed with `abuild-sign`. There is
no container runtime, no cross-compiler and no distro toolchain to maintain - the
build host only needs root, `tar`, and `qemu-user` for the foreign architectures.

## Using the repository

On a UFLinux system (the exact commands are also on the landing page of the
published site):

```sh
REPO="https://UFlinux-project.github.io/uflinux3-apps/apk/v3.24/uflinux-apps"
doas wget -qO /etc/apk/keys/uflinux3.ec384.pub "$REPO/uflinux3.ec384.pub"
# apk reads top-level *.list files from this directory, but no package creates it.
doas mkdir -p /etc/apk/repositories.d
printf '%s\n' "$REPO" | doas tee /etc/apk/repositories.d/uflinux-apps.list >/dev/null
doas apk update
doas apk add photocraft web-browser
```

Packages are signed with an ECDSA key over P-384, the same keypair UFLinux itself
uses, so adding this repository does not introduce a second root of trust. The
member carrying the signature is still named `.SIGN.RSA256.`: apk-tools takes the
digest from that name and the only one it accepts there is SHA-256, which an EC key
signs fine - see the `sigtype` comment in `scripts/build.py`. The key file is a
bare armoured public key, not a certificate; apk has no certificate chain to
validate. `config/repository.json` records the key id, the Alpine version the
packages are built against, and the path the repository is published under.

## What is here

| Package | Upstream | Notes |
| --- | --- | --- |
| `anicraft`, `cadcraft`, `deckcraft`, `designcraft`, `effectcraft`, `figcraft`, `filmcraft`, `lightcraft`, `materialcraft`, `pdfcraft`, `photocraft`, `soundcraft`, `texturecraft`, `vectorcraft`, `wordcraft` | [Craft apps](https://getartcraft.com/apps) | Rust, always the newest upstream release |
| `web-browser` | [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) on Alpine's `chromium` recipe | Shown as **Web Browser**, Papirus icon; `x86_64` and `aarch64` only |

Craft Anything and ArtCraft Studio are deliberately not packaged here.

Packages are built for every architecture UFLinux ships: `x86`, `x86_64`,
`aarch64`, `armv7`, `ppc64le`, `s390x` and `riscv64`. Shared libraries come from
Alpine wherever a compatible package exists (`ffmpeg`, `openh264`, `pipewire`,
`mesa`, `fontconfig`, `icu`, ...), so an installed app pulls dependencies that
the base system already provides instead of carrying its own copies.

## Layout

```
aports/apps/<pkgname>/APKBUILD   one recipe per package (+ patches and data files)
aports/apps/craft-common.sh      shared phases for the fifteen Craft recipes
aports/.shellcheckrc             shellcheck knobs for sourced recipes (no shebang)
config/architectures.json        architecture -> runner, qemu-user, qemu machine
config/repository.json           Alpine version, rootfs locks, publish path, signing key
config/apps.lock.json            upstream tag, tarball hash and metadata per Craft app
scripts/build.py                 the build driver: chroot, abuild, index, Pages payload
scripts/build.sh                 thin wrapper so `sh scripts/build.sh` works
scripts/ci-matrix.py             the GitHub Actions matrix, from the config files
scripts/gen-app-recipes.py       renders the Craft APKBUILDs from the lockfile
scripts/update-checksums.py      bumps the lockfile to the newest upstream release
scripts/update-browser.py        bumps the web-browser recipe to Alpine's chromium
scripts/collect-packages.py      turns workflow artifacts back into out/packages/<arch>/
.github/workflows/build.yml      build every package, then publish the repository
.github/workflows/update.yml     daily: bump the lockfile and browser, release if upstream moved
```

The fifteen Craft recipes are generated, not hand-written: `config/apps.lock.json`
is the input and `scripts/gen-app-recipes.py` is the formatter. CI regenerates
them and fails if the result differs from what is committed, so a recipe can
never drift from the lockfile.

## Building locally

`scripts/build.py` runs on any Linux machine where you can be root; the checkout
does not need Alpine on it.

```sh
# One package, native architecture: unpacks .state/x86_64/root, writes out/packages/x86_64/
sudo scripts/build.sh --arch x86_64 --package anicraft --stage packages

# Everything for one architecture, then index it
sudo scripts/build.sh --arch x86_64

# Index and sign whatever is already in out/packages/, and produce out/pages/
sudo scripts/build.sh --stage index
```

Useful details:

- An `--arch` the host cannot execute natively needs `qemu-user-binfmt` with the
  `F` (fix-binary) flag; `build.py` refuses to start rather than fail obscurely
  later. Native means the host's own architecture plus any 32-bit variant its
  kernel can run directly, so an `x86_64` host builds `x86` and `x86_64` without
  emulation and an `aarch64` host builds `aarch64` and `armv7` - which is how
  Alpine's own builders do it. The table lives in `NATIVE` in `scripts/build.py`.
- Downloads, the apk cache, `CARGO_HOME` and `CARGO_TARGET_DIR` live under
  `--cache` (default `.cache/build/<arch>`), so re-running a build is incremental
  and safe to delete at any time.
- `--workdir` is disposable per architecture; if the rootfs lock in
  `config/repository.json` changes, remove it and start again.
- Both `--workdir` and `--cache` default to somewhere under the checkout and both
  accept any path, so a build can live entirely on another filesystem - a
  `tmpfs` is fast and self-cleaning, at the cost of the ~5 GiB a Craft build and
  its target directory need. `build.py` refuses to build if the target has less
  than 10 GiB free; `--stage index` needs almost nothing and does not ask.
- Set `APK_SIGNING_PRIVATE_KEY` and `APK_SIGNING_PUBLIC_KEY` whenever packages are
  built, not only when they are published: every package is signed as it is built
  and the index verifies those signatures, so a build without the key produces
  packages the index rejects. Without them a throwaway key is generated, which is
  fine for testing one recipe on one machine and useless to a client.
- `SOURCE_DATE_EPOCH` is honoured, so CI can produce bit-for-bit reproducible
  indexes.

Before pushing, the checks CI runs are all local: `python3 scripts/gen-app-recipes.py`
(recipes must match the lockfile), `python3 scripts/update-checksums.py --check`,
`python3 scripts/update-browser.py --check`,
`shellcheck` plus `busybox ash -n` on every shell file under `aports/` and
`scripts/` - recipes, the helper they source, and the scripts a recipe installs
alike, because abuild is `ash`, not `bash`, and a bashism only shows up as a
failed build - and `actionlint`.
See [docs/adding-an-app.md](docs/adding-an-app.md) for the details.

## Releasing

Every day, `.github/workflows/update.yml` runs `scripts/update-checksums.py` for
the Craft apps and `scripts/update-browser.py` for the browser (which follows
Alpine's `community/chromium` recipe rather than a release feed - see
[docs/browser.md](docs/browser.md)). If nothing moved, that is the end of it. If
anything did, the workflow commits the new lockfile, its regenerated recipes and
the browser recipe to `main`, tags the commit, and starts a release of it - so an
upstream release becomes a package here without anyone noticing it first.

Tags are dates, not versions: `v<year>.<month>.<number-in-month>`. No single
number describes fifteen apps, and the build workflow only requires a tag that
looks like `v<major>.<minor>.<patch>`, which that satisfies. Pushing such a tag
by hand releases whatever it points at, so a hand-made tag is still the way to
rebuild an existing set of versions. The weekly schedule and a manual run build
and publish the current `main` without a tag. A push to `main` or a pull request
builds `x86_64` only and does not publish: it is the cheap check that the recipes
and the index still work, and a pull request from a fork could not read the
signing key anyway - publishing with a different one would lock existing clients
out. Publishing replaces the whole repository, so it only ever happens from a run
that built every architecture.

The automatic bump has no review step, so the trade is that a bad bump turns the
matrix run red rather than a pull request yellow. What limits the damage is that
releasing and publishing are separate: a bump whose build fails leaves the
previously published repository in place, and the tag stays as the record of what
was attempted. Nothing retries on its own - the lockfile is already current, so
the next day's check finds nothing to do - which means a red release run is the
thing to watch, and fixing the recipe and re-tagging is a human job. If upstream
is unreachable, or one app's release turns out to be mis-tagged, the update
workflow fails without committing anything, naming the app.

Publication is all or nothing: one failing shard fails the build stage, which
skips `publish`. Shards do not cancel each other (`fail-fast: false`), so a run
reports every architecture that is broken rather than the first one. A run that
would publish but cannot read the signing key fails in the first job, before any
package is built, because packages are signed as they are built and the index will
not describe one it cannot verify.

See [docs/browser.md](docs/browser.md) for the Chromium pipeline and how to bump
it, and [docs/adding-an-app.md](docs/adding-an-app.md) to add or update a package.
