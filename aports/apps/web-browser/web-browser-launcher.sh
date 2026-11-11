#!/bin/sh
# Launcher for Web Browser, forked from Alpine's chromium-launcher.sh.
#
# The upstream binary is still called chromium and lives next to this script in
# /usr/lib/web-browser; /usr/bin/web-browser is a symlink to this file, which is
# why CHROME_WRAPPER resolves to this directory.

for f in /etc/web-browser/*.conf; do
  # shellcheck source=/dev/null disable=SC1091
  [ -f "$f" ] && . "$f"
done

# Append WEB_BROWSER_USER_FLAGS (from env) on top of the system defaults in
# /etc/web-browser/web-browser.conf.
WEB_BROWSER_FLAGS="$WEB_BROWSER_FLAGS ${WEB_BROWSER_USER_FLAGS:+"$WEB_BROWSER_USER_FLAGS"}"

# Let the wrapped binary know that it has been run through the wrapper
CHROME_WRAPPER=$(readlink -f "$0")
export CHROME_WRAPPER

PROGDIR=${CHROME_WRAPPER%/*}

case ":$PATH:" in
*:$PROGDIR:*)
  # $PATH already contains $PROGDIR
  ;;
*)
  # Append $PROGDIR to $PATH
  export PATH="$PATH:$PROGDIR"
  ;;
esac

if [ "$(id -u)" -eq 0 ] &&
  [ "$(stat -c %u -L "${XDG_CONFIG_HOME:-$HOME}")" -eq 0 ]; then
  # Running as root with HOME owned by root.
  # Pass --user-data-dir to work around upstream failsafe.
  WEB_BROWSER_FLAGS="--user-data-dir=${XDG_CONFIG_HOME:-"$HOME"/.config}/chromium $WEB_BROWSER_FLAGS"
fi

# Hardware video decode: prefer VA-API, fall back to the experimental V4L2
# backend. The binary is built with use_v4l2_codec=true, but the runtime feature
# that actually selects it (PreferV4L2VideoAcceleration) is
# FEATURE_DISABLED_BY_DEFAULT, so nothing uses V4L2 unless we ask for it here.
#
# VA-API counts as available only when both halves of it are there: a DRM render
# node to talk to, and a driver for libva to load. libva dlopen()s
# <search-path>/*_drv_video.so, where the search path is $LIBVA_DRIVERS_PATH or
# else the loader's dri directory, so that file is what we look for. Probing
# with vainfo instead would not work: it needs a display connection and exits
# non-zero when headless, which would make it impossible to tell "no GPU" apart
# from "no X or Wayland".
#
# So: VA-API present -> launch unmodified and let Chromium use it. VA-API absent
# but a /dev/video node present -> opt into V4L2. Neither present -> launch
# unmodified and let Chromium fall back to software.
vaapi_available() {
  local node dir drv
  # libva takes $LIBVA_DRIVERS_PATH as a colon-separated list, so split it the
  # same way. A function-local IFS is restored when we return.
  local IFS=:

  for node in /dev/dri/renderD*; do
    [ -e "$node" ] || continue
    for dir in ${LIBVA_DRIVERS_PATH:-} /usr/lib/dri /usr/lib64/dri; do
      for drv in "$dir"/*_drv_video.so; do
        [ -e "$drv" ] && return 0
      done
    done
    # A render node with no driver anywhere is not going to get one from
    # looking at the next render node either.
    break
  done
  return 1
}

if ! vaapi_available; then
  for node in /dev/video*; do
    [ -e "$node" ] || continue
    WEB_BROWSER_FLAGS="--enable-features=PreferV4L2VideoAcceleration $WEB_BROWSER_FLAGS"
    break
  done
fi

# Set the .desktop file name. This is what the browser looks for when it tries
# to find its own entry in the application menu, so it has to match the name of
# the desktop file we install.
export CHROME_DESKTOP="web-browser.desktop"
export CHROME_VERSION_EXTRA="uflinux3"

exec "$PROGDIR/chromium" ${WEB_BROWSER_FLAGS} "$@"
