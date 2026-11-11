# shellcheck shell=sh
# Shared build/staging logic for every Craft app in this repository.
#
# Sourced by each aports/apps/<app>/APKBUILD, one level below this file:
#
#   . "$startdir/../craft-common.sh"
#
# $startdir is set by abuild before it sources the APKBUILD, so the helper can
# live in the shared parent directory: one copy for all apps, and no entry in
# $source (abuild only warns about unlisted files inside $startdir itself).
#
# An APKBUILD must set before sourcing:
#   _craft_app       upstream crate/binary stem, e.g. photocraft
#   _craft_bins      space-separated binaries to install, e.g. "photocraft photocraft-cli"
#
# and may set:
#   _craft_features  space-separated cargo features (e.g. heif)
#   _craft_man       "yes" if the repo has a man-and-completions example
#   _craft_extra_mk  extra makedepends for this app
#   _craft_extra_dep extra runtime depends for this app
#
# Why we do not call upstream's packaging/linux/package.sh: it builds glibc
# .deb/.rpm/AppImage artifacts with nfpm and appimagetool. We only want the FHS
# tree it stages, so that part is reimplemented here against abuild's $pkgdir.

_craft_app_id="ai.storyteller.$_craft_app"
_craft_upstream="packaging/linux"
_craft_hicolor="assets/app-icon/hicolor"

# Upstream's own workaround for building under emulation: glibc's malloc tuning
# interacts badly with qemu-user. Harmless on musl, kept for parity with the
# upstream release workflow.
export MALLOC_TRIM_THRESHOLD_=131072

# Deliberately left unset: CRAFT_FONTS_DIR, CRAFT_FONTS_REQUIRED, CRAFT_I18N_DIR.
# Upstream's release workflow checks out storytold/craft-fonts and
# storytold/craft-i18n and points these at them, which embeds Inter and
# JetBrains Mono in the UI and translates the interface. We let build.rs fall
# back to its bundled fonts instead, so every build prints one warning:
#
#	warning: UI fonts: no craft-fonts found (set CRAFT_FONTS_DIR ...)
#
# That warning is expected, not a defect. docs/adding-an-app.md explains why.

# Upstream stamps the AppStream release with the build date. Derive it once from
# SOURCE_DATE_EPOCH so the package stays reproducible.
_craft_date=$(date -u -d "@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y-%m-%d)

# The version lives in exactly one place upstream: [workspace.package] in the root
# Cargo.toml. Fail loudly if our pkgver drifted from it (a mis-tagged release, or a
# lockfile that was not regenerated) rather than shipping metadata that disagrees
# with the binary.
_craft_check_version() {
	local got
	got=$(awk '
		/^\[/ { in_pkg = ($0 == "[workspace.package]") ; next }
		in_pkg && $1 == "version" { gsub(/[" ]/, "", $3); print $3; exit }
	' "$builddir/Cargo.toml")
	if [ "$got" != "$pkgver" ]; then
		echo "ERROR: pkgver=$pkgver but $_craft_app Cargo.toml says version=$got" >&2
		return 1
	fi
}

prepare() {
	default_prepare
	_craft_check_version
}

build() {
	# Upstream reads build metadata from these; the archive tarball has no .git,
	# so the commit is unknown and we pass the tag instead. BUILD_DATE comes from
	# SOURCE_DATE_EPOCH so the metainfo release date is reproducible.
	# abuild runs on ash, which has no ${var^^}, so the prefix is uppercased
	# with tr. App names are lowercase ASCII by construction.
	local prefix
	prefix=$(printf '%s' "$_craft_app" | tr '[:lower:]' '[:upper:]')
	eval "export ${prefix}_BUILD_SHA=\${${prefix}_BUILD_SHA:-$pkgver}"
	eval "export ${prefix}_BUILD_DATE=\${${prefix}_BUILD_DATE:-$_craft_date}"

	local feats=
	if [ -n "$_craft_features" ]; then
		feats="--features $(printf '%s' "$_craft_features" | tr ' ' ',')"
	fi

	# shellcheck disable=SC2046,SC2086  # feature/bin lists are intentionally word-split
	cargo build --release --locked \
		$(for _b in $_craft_bins; do printf -- '-p %s ' "$_b"; done) \
		$feats --bins

	if [ "$_craft_man" = yes ]; then
		# Builds the example that generates the CLI's man page and completions.
		# The separate cargo run is upstream's own arrangement, copied from
		# packaging/linux/package.sh, and it is not an oversight: the example's
		# dev-dependencies (clap_complete, clap_mangen, and in photocraft's case
		# the whole test-only graph) would otherwise have their features unified
		# into the shipped binaries. The cost is real - a second resolution
		# rebuilds a large part of the graph, measured at 163 crates for
		# photocraft - and merging the two invocations into
		# `--bins --example man-and-completions` would save it and quietly change
		# which features the binary is compiled with.
		# shellcheck disable=SC2086
		cargo build --release --locked \
			-p "$_craft_app-cli" $feats --example man-and-completions
	fi
}

package() {
	local bin
	# Respect an out-of-tree CARGO_TARGET_DIR: the CI driver points it at a
	# bind-mounted cache so incremental rebuilds survive between runs.
	local bindir="${CARGO_TARGET_DIR:-$builddir/target}/release"

	for bin in $_craft_bins; do
		install -Dm755 "$bindir/$bin" "$pkgdir/usr/bin/$bin"
	done

	# Desktop entry, MIME package and AppStream metadata all come straight from
	# upstream, so translations, keywords and icon names stay in sync with the
	# upstream Flatpak/deb packages.
	install -Dm644 "$_craft_upstream/$_craft_app_id.desktop" \
		"$pkgdir/usr/share/applications/$_craft_app_id.desktop"
	if [ -f "$_craft_upstream/$_craft_app_id.mime.xml" ]; then
		install -Dm644 "$_craft_upstream/$_craft_app_id.mime.xml" \
			"$pkgdir/usr/share/mime/packages/$_craft_app_id.xml"
	fi
	if [ -f "$_craft_upstream/$_craft_app_id.metainfo.xml.in" ]; then
		mkdir -p "$pkgdir/usr/share/metainfo"
		sed -e "s/@VERSION@/$pkgver/g" -e "s/@DATE@/$_craft_date/g" \
			"$_craft_upstream/$_craft_app_id.metainfo.xml.in" \
			>"$pkgdir/usr/share/metainfo/$_craft_app_id.metainfo.xml"
	fi

	# Upstream ships a complete hicolor tree keyed on the app id, so the icon
	# matches the desktop file's Icon= on every theme that inherits hicolor.
	cp -R "$_craft_hicolor" "$pkgdir/usr/share/icons/"

	mkdir -p "$pkgdir/usr/share/doc/$_craft_app"
	local doc
	for doc in README.md LICENSE LICENSE-MIT LICENSE-APACHE COPYRIGHT NOTICE; do
		[ -f "$doc" ] && cp "$doc" "$pkgdir/usr/share/doc/$_craft_app/"
	done

	if [ "$_craft_man" = yes ]; then
		# The example is a program, not a directory: it takes one argument, an
		# output directory, and writes the man pages and completions into it.
		# Upstream's own packaging.sh runs it the same way. $tmpdir is created
		# by default_prepare() and skipped by abuild's stray-file check.
		local gen="$bindir/examples/man-and-completions" out="$tmpdir/man"
		[ -x "$gen" ] || {
			echo "ERROR: $_craft_app-cli's man-and-completions example is" \
				"missing; the -doc and -completion subpackages have no input" >&2
			return 1
		}
		mkdir -p "$out"
		"$gen" "$out"
		install -Dm644 -t "$pkgdir/usr/share/man/man1" "$out"/*.1
		# The completion files are named the way Alpine's completion subpackages
		# expect: the command name, no suffix for bash, an underscore prefix for
		# zsh. clap_complete also writes elvish and PowerShell, which have no
		# home in an Alpine package.
		install -Dm644 "$out/$_craft_app-cli.bash" \
			"$pkgdir/usr/share/bash-completion/completions/$_craft_app-cli"
		install -Dm644 "$out/_$_craft_app-cli" \
			"$pkgdir/usr/share/zsh/site-functions/_$_craft_app-cli"
		install -Dm644 "$out/$_craft_app-cli.fish" \
			"$pkgdir/usr/share/fish/vendor_completions.d/$_craft_app-cli.fish"
	fi

	# Same validators upstream CI uses: a broken desktop entry or metainfo file
	# fails the build here rather than in the user's application menu.
	if command -v desktop-file-validate >/dev/null; then
		desktop-file-validate "$pkgdir/usr/share/applications/$_craft_app_id.desktop"
	fi
	if command -v appstreamcli >/dev/null; then
		appstreamcli validate --no-net --explain \
			"$pkgdir/usr/share/metainfo/$_craft_app_id.metainfo.xml"
	fi
}
