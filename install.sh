#!/bin/bash
# WoW Mentor installer. Paste into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.sh | bash
# It downloads the app into ~/.wowmentor/app, then runs `wowmentor setup`. Safe to run again to update.
# Extra words after `bash -s --` are passed to `wowmentor setup` (for example --wow-dir "/path/to/_forever_").
#
# Environment overrides (all optional; mainly for testing and for pinning a release):
#   WOWMENTOR_REPO         owner/name on GitHub              (default PhilipAD/wow-mentor)
#   WOWMENTOR_REF          branch, tag or commit to download (default main; for example v1.0.0)
#   WOWMENTOR_DIR          where the program is installed    (default ~/.wowmentor/app)
#   WOWMENTOR_TARBALL_URL  download this .tar.gz instead of GitHub (https://, or file:// for a local file)
#   WOWMENTOR_SRC          copy this local folder instead of downloading anything
set -euo pipefail

REPO="${WOWMENTOR_REPO:-PhilipAD/wow-mentor}"
REF="${WOWMENTOR_REF:-main}"
DEST="${WOWMENTOR_DIR:-$HOME/.wowmentor/app}"

say() { printf '%s\n' "$*"; }

if [ "$(uname -s)" != "Darwin" ]; then
  say "WoW Mentor needs a Mac: it reads the game window with a macOS API."
  exit 1
fi

# On a Mac without Apple's Command Line Tools, /usr/bin/python3 exists but is only a stub that
# opens an install window, so "command -v python3" alone is not proof that Python works.
clt_help() {
  say "Python 3 is not ready yet. macOS will now offer to install Apple's free Command Line Tools."
  say "Click Install, wait for it to finish (a few minutes), then paste the same command again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
}
if ! command -v python3 >/dev/null 2>&1; then
  clt_help
fi
if ! py_ok="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)' 2>/dev/null)"; then
  clt_help
fi
if [ "$py_ok" != "1" ]; then
  say "Python 3.9 or newer is needed (yours is older)."
  say "Run:  xcode-select --install   wait for it to finish, and then paste the same command again."
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ -n "${WOWMENTOR_SRC:-}" ]; then
  [ -f "$WOWMENTOR_SRC/wowmentor.sh" ] || { say "WOWMENTOR_SRC ($WOWMENTOR_SRC) has no wowmentor.sh."; exit 1; }
  say "Copying WoW Mentor from $WOWMENTOR_SRC ..."
  mkdir -p "$tmp/wow-mentor"
  # Copy everything except version-control data and caches.
  (cd "$WOWMENTOR_SRC" && tar --exclude='./.git' --exclude='__pycache__' --exclude='./.venv' -cf - .) | tar -xf - -C "$tmp/wow-mentor"
else
  url="${WOWMENTOR_TARBALL_URL:-https://github.com/$REPO/archive/$REF.tar.gz}"
  say "Downloading WoW Mentor ($REPO@$REF)..."
  if ! curl -fsSL "$url" -o "$tmp/download.tar.gz"; then
    say "The download failed. Check your internet connection and paste the same command again."
    say "If it keeps failing, see docs/TROUBLESHOOTING.md or open an issue."
    exit 1
  fi
  tar -xzf "$tmp/download.tar.gz" -C "$tmp" || { say "The download is not a valid archive. Please try again."; exit 1; }
  rm -f "$tmp/download.tar.gz"
fi
src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -n "$src" ] && [ -f "$src/wowmentor.sh" ] || { say "Download looks wrong (no wowmentor.sh). Please report this."; exit 1; }

case "$DEST" in
  ""|"/"|"$HOME"|"$HOME/") say "Refusing to install into '$DEST'. Unset WOWMENTOR_DIR or point it at an empty folder."; exit 1 ;;
esac
rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
mv "$src" "$DEST"
chmod +x "$DEST/wowmentor.sh" "$DEST/wowui"

mkdir -p "$HOME/.local/bin"
ln -sf "$DEST/wowmentor.sh" "$HOME/.local/bin/wowmentor"
ln -sf "$DEST/wowui" "$HOME/.local/bin/wowui"

say ""
say "Running setup..."
say ""
"$DEST/wowmentor.sh" setup "$@"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    say ""
    say "Tip: to run 'wowmentor' from any folder, add this line to ~/.zshrc and open a new Terminal:"
    # shellcheck disable=SC2016  # the dollar signs are meant literally: this is text for the user to copy
    say '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac
