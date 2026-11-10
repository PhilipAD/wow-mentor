#!/bin/bash
# Test install.sh without GitHub and without touching your real home folder.
# Everything happens in a temporary HOME. On a Mac it also runs a real `wowmentor setup --no-launchd --no-build`.
#   scripts/test_install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

export HOME="$SANDBOX/home"
export WOWMENTOR_HOME="$SANDBOX/config"
export WOWMENTOR_LOGS="$SANDBOX/logs"
mkdir -p "$HOME"

# A PATH with only the basic tools install.sh needs (no python3), plus a fake uname.
mkbin() {   # mkbin <dir> <uname output> [with-python]
  mkdir -p "$1"
  for t in bash sh mktemp tar find head rm mkdir mv chmod ln dirname cp cat curl printf env; do
    p="$(command -v "$t" 2>/dev/null || true)"
    case "$p" in /*) ln -sf "$p" "$1/$t" ;; esac
  done
  printf '#!/bin/sh\necho %s\n' "$2" > "$1/uname"; chmod +x "$1/uname"
  if [ "${3:-}" = "with-python" ]; then ln -sf "$(command -v python3)" "$1/python3"; fi
}

# 1. Not a Mac: refuse politely.
mkbin "$SANDBOX/bin-linux" Linux with-python
out="$(PATH="$SANDBOX/bin-linux" bash "$ROOT/install.sh" 2>&1)" && fail "should refuse on Linux"
echo "$out" | grep -q "needs a Mac" || fail "no Mac message: $out"
ok "refuses on a non-Mac"

# 2. No python3: say what to do (xcode-select is faked so nothing opens).
mkbin "$SANDBOX/bin-nopy" Darwin
printf '#!/bin/sh\necho xcode-select called >> "%s/xcode.log"\n' "$SANDBOX" > "$SANDBOX/bin-nopy/xcode-select"; chmod +x "$SANDBOX/bin-nopy/xcode-select"
out="$(PATH="$SANDBOX/bin-nopy" bash "$ROOT/install.sh" 2>&1)" && fail "should fail without python3"
echo "$out" | grep -q "Command Line Tools" || fail "no python message: $out"
[ -f "$SANDBOX/xcode.log" ] || fail "did not offer the Command Line Tools"
[ ! -e "$HOME/.local" ] && [ ! -e "$HOME/.wowmentor" ] || fail "touched HOME before checks passed"
ok "no python3 message, nothing installed"

# 3. A python3 that exists but is only the macOS stub (exits non-zero).
mkbin "$SANDBOX/bin-stub" Darwin
printf '#!/bin/sh\nexit 1\n' > "$SANDBOX/bin-stub/python3"; chmod +x "$SANDBOX/bin-stub/python3"
cp "$SANDBOX/bin-nopy/xcode-select" "$SANDBOX/bin-stub/xcode-select"
out="$(PATH="$SANDBOX/bin-stub" bash "$ROOT/install.sh" 2>&1)" && fail "should fail with a stub python3"
echo "$out" | grep -q "Command Line Tools" || fail "stub python message: $out"
ok "stub python3 handled"

# 4. A bad download: clear message, nothing installed.
mkbin "$SANDBOX/bin-mac" Darwin with-python
out="$(PATH="$SANDBOX/bin-mac" WOWMENTOR_TARBALL_URL="file://$SANDBOX/missing.tar.gz" bash "$ROOT/install.sh" 2>&1)" && fail "missing tarball should fail"
echo "$out" | grep -q "download failed" || fail "no download message: $out"
echo "not a tarball" > "$SANDBOX/bad.tar.gz"
out="$(PATH="$SANDBOX/bin-mac" WOWMENTOR_TARBALL_URL="file://$SANDBOX/bad.tar.gz" bash "$ROOT/install.sh" 2>&1)" && fail "bad tarball should fail"
echo "$out" | grep -q "not a valid archive" || fail "no archive message: $out"
[ ! -e "$HOME/.wowmentor" ] || fail "partial install left behind"
ok "bad downloads fail cleanly"

# 5. Refuses a dangerous install folder.
out="$(PATH="$SANDBOX/bin-mac" WOWMENTOR_SRC="$ROOT" WOWMENTOR_DIR="$HOME" bash "$ROOT/install.sh" 2>&1)" && fail "should refuse DEST=HOME"
echo "$out" | grep -q "Refusing" || fail "no refusal: $out"
ok "refuses to install into the home folder"

# 6. Full run on a Mac (setup itself is macOS only).
if [ "$(uname -s)" = "Darwin" ]; then
  WOW="$SANDBOX/World of Warcraft/_forever_"
  mkdir -p "$WOW/Interface/AddOns"
  # A real tarball, as GitHub makes it: one top-level folder.
  mkdir "$SANDBOX/tarsrc"
  (cd "$ROOT" && tar --exclude='./.git' --exclude='__pycache__' -cf - .) | (mkdir "$SANDBOX/tarsrc/wow-mentor-main" && tar -xf - -C "$SANDBOX/tarsrc/wow-mentor-main")
  tar -czf "$SANDBOX/release.tar.gz" -C "$SANDBOX/tarsrc" wow-mentor-main
  for mode in tarball src; do
    rm -rf "$HOME/.wowmentor" "$HOME/.local" "$WOWMENTOR_HOME"
    if [ "$mode" = tarball ]; then export WOWMENTOR_TARBALL_URL="file://$SANDBOX/release.tar.gz"; unset WOWMENTOR_SRC
    else export WOWMENTOR_SRC="$ROOT"; unset WOWMENTOR_TARBALL_URL; fi
    out="$(PATH="$SANDBOX/bin-mac:/usr/bin:/bin" bash "$ROOT/install.sh" --wow-dir "$WOW" --no-launchd --no-build 2>&1)" || fail "install ($mode) failed: $out"
    echo "$out" | grep -q "1/5  Found WoW at" || fail "no setup output ($mode): $out"
    echo "$out" | grep -q "Added the wowmentor and wowui commands" || fail "no PATH message ($mode)"
    grep -qF '.local/bin' "$HOME/.zshrc" || fail "PATH line not written to .zshrc ($mode)"
    # shellcheck disable=SC2016  # the dollar signs are literal text that was written to .zshrc
    [ "$(grep -cF 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.zshrc")" = 1 ] || fail "PATH line duplicated ($mode)"
    [ -x "$HOME/.wowmentor/app/wowmentor.sh" ] || fail "program not installed ($mode)"
    [ "$(readlink "$HOME/.local/bin/wowmentor")" = "$HOME/.wowmentor/app/wowmentor.sh" ] || fail "symlink wrong ($mode)"
    [ -d "$WOW/Interface/AddOns/WoWFeed" ] && [ -d "$WOW/Interface/AddOns/WFSlot1000" ] || fail "addon not installed ($mode)"
    [ ! -e "$HOME/.wowmentor/app/.git" ] || fail ".git copied ($mode)"
    [ ! -f "$HOME/Library/LaunchAgents/com.wowmentor.server.plist" ] || fail "launchd touched ($mode)"
    "$HOME/.local/bin/wowmentor" --version | grep -q "^wowmentor " || fail "wowmentor --version ($mode)"
    links_out="$("$HOME/.local/bin/wowmentor" links)" || fail "links ($mode)"      # not piped into grep -q: that closes the pipe early and flakes with BrokenPipe
    printf '%s\n' "$links_out" | grep -q "Paste this to your AI agent" || fail "links text ($mode)"
    ok "full install via $mode, re-run keeps the config"
    cp "$WOWMENTOR_HOME/config.json" "$SANDBOX/config.before"
    PATH="$SANDBOX/bin-mac:/usr/bin:/bin" bash "$ROOT/install.sh" --wow-dir "$WOW" --no-launchd --no-build >/dev/null 2>&1 || fail "second install ($mode) failed"
    cmp -s "$WOWMENTOR_HOME/config.json" "$SANDBOX/config.before" || fail "re-install changed the secrets ($mode)"
  done
  # The double-click installer, run from an unzipped copy (extra words go to setup, so the test can add the safe flags).
  rm -rf "$HOME/.local" "$WOWMENTOR_HOME"
  cp -R "$SANDBOX/tarsrc/wow-mentor-main" "$SANDBOX/unzipped"
  out="$(PATH="/usr/bin:/bin" bash "$SANDBOX/unzipped/Install WoW Mentor.command" --wow-dir "$WOW" --no-launchd --no-build < /dev/null 2>&1)" || true
  echo "$out" | grep -q "1/5  Found WoW at" || fail ".command did not run setup: $out"
  [ "$(readlink "$HOME/.local/bin/wowmentor")" = "$SANDBOX/unzipped/wowmentor.sh" ] || fail ".command symlink wrong"
  ok "double-click installer"
else
  echo "skip: full install (needs macOS)"
fi
echo "install.sh tests passed"
