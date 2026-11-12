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

# Set the .desktop file name. This is what the browser looks for when it tries
# to find its own entry in the application menu, so it has to match the name of
# the desktop file we install.
export CHROME_DESKTOP="web-browser.desktop"
export CHROME_VERSION_EXTRA="uflinux3"

exec "$PROGDIR/chromium" ${WEB_BROWSER_FLAGS} "$@"
