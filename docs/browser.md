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
- **Experimental V4L2 backend, selected by the launcher.**
  `use_v4l2_codec=true` builds Chromium's Video4Linux2 hardware codec backend
  (`media/gpu/v4l2`) in addition to VA-API. It is upstream-labelled experimental
  and is not used unless the `PreferV4L2VideoAcceleration` feature is on, which
  it is not by default, so VA-API remains the default accelerator. The launcher
  supplies the opt-in: when VA-API is *not* available but a `/dev/video*` node
  is, it prepends `--enable-features=PreferV4L2VideoAcceleration`.
  VA-API counts as available only with both a DRM render node *and* a driver for
  libva to load, i.e. a `*_drv_video.so` in `$LIBVA_DRIVERS_PATH` or the
  loader's `dri` directory — which is exactly what libva itself `dlopen()`s.
  Shelling out to `vainfo` would not do: it needs a display connection and fails
  headless, so it cannot tell "no GPU" apart from "no X or Wayland". Machines
  with VA-API, and machines with neither, are launched unmodified. To override
  the probe, put `--disable-features=PreferV4L2VideoAcceleration` (or
  `--enable-features=` it) in `WEB_BROWSER_USER_FLAGS`; user flags come last on
  the command line and `--disable-features` wins over `--enable-features`. The
  backend pulls in no new build dependency: it links only libdrm (unbundled),
  in-tree `libyuv`/`libgav1`, and `linux/videodev2.h` from `linux-headers`.
- **Default enterprise policy.** `uflinux3-web-browser.json` is installed into
  `/etc/chromium/policies/recommended/`. This build reads policies from
  `/etc/chromium/policies` (`components/policy/core/common/policy_paths.cc`: on
  Linux, without Google Chrome branding, that directory is `/etc/chromium/policies`,
  not `/etc/web-browser`), and `config_dir_policy_loader.cc` treats the
  `recommended/` subdir as `POLICY_LEVEL_RECOMMENDED` -- so these are defaults the
  user can still change in Settings, without the "Managed by your organization"
  lock a mandatory (`managed/`) policy would impose. The namespaced file name
  keeps it clear of Alpine's own chromium package, which writes into the same
  directory. It turns Memory Saver
  on at maximum aggressiveness (`HighEfficiencyModeEnabled`,
  `MemorySaverModeSavings=2`) and enables extended preloading
  (`NetworkPredictionOptions=3`; the handler is a plain integer with no range
  clamp, so the value 3 that the UI calls "Extended" is accepted).
- **Two extensions installed by default.** The same policy file pre-installs
  enhanced-h264ify (`omkfmpieigblcllmkgbflkikinpkodlk`) and uBlock Origin Lite
  (`ddkjiahejlhfcafbddmgiahcphecmpfh`) from the Chrome Web Store update
  endpoint. `installation_mode: normal_installed` rather than a force-install
  list, so both show up as installed on first run but stay removable. uBlock
  Origin Lite is the MV3 build: the classic uBlock Origin
  (`cjpalhdlnbpafiamejdnhcphjbkeiagm`) is MV2 and cannot be shipped here.
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

## Bumping the version

Chromium is one atomic bump; the components below are pinned to each other and a
mismatch fails in `prepare()` or during the build, sometimes in confusing ways.

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

Then re-vendor the Alpine-side files, because they change between releases: the
20 `*.patch` files, the `_copium_patches` list, and any `source` entry whose URL
shape changed. Copy them from the recipe of the release you are bumping to, and
keep the local renames (`web-browser-*` for our own files, `.noauto` for anything
that must not be unpacked or auto-applied).

Finally regenerate `sha512sums`. The list is positional: abuild matches it
against `$source` entry by entry, and the name it hashes is the left-hand side of
`::`, so an entry that is out of order or renamed fails loudly rather than
silently.

```sh
# From a checkout, inside an Alpine chroot with coreutils and curl:
abuild -F checksum            # fetches everything and prints the new block
```

`abuild` also warns about any file in the recipe directory that is not listed in
`$source`, so leftover patches from the previous version show up here too.
