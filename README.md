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
scripts/collect-packages.py      turns workflow artifacts back into out/packages/<arch>/
.github/workflows/build.yml      build every package, then publish the repository
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
`shellcheck` plus `busybox ash -n` on every shell file under `aports/` and
`scripts/` - recipes, the helper they source, and the scripts a recipe installs
alike, because abuild is `ash`, not `bash`, and a bashism only shows up as a
failed build - and `actionlint`.
See [docs/adding-an-app.md](docs/adding-an-app.md) for the details.

## Releasing

Push a tag `v<major>.<minor>.<patch>` and the workflow builds every package for
every architecture and publishes the result to GitHub Pages. The weekly schedule
and a manual run do the same thing without the tag. A push to `main` or a pull
request builds `x86_64` only and does not publish: it is the cheap check that the
recipes and the index still work, and a pull request from a fork could not read
the signing key anyway - publishing with a different one would lock existing
clients out. Publishing replaces the whole repository, so it only ever happens
from a run that built every architecture.

Publication is all or nothing: one failing shard fails the build stage, which
skips `publish` and leaves the previously published repository in place. Shards do
not cancel each other (`fail-fast: false`), so a run reports every architecture
that is broken rather than the first one. A run that would publish but cannot read
the signing key fails in the first job, before any package is built, because
packages are signed as they are built and the index will not describe one it cannot
verify.

See [docs/browser.md](docs/browser.md) for the Chromium pipeline and how to bump
it, and [docs/adding-an-app.md](docs/adding-an-app.md) to add or update a package.
