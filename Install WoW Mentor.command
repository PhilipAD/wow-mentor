#!/bin/bash
# Double-click this file (after downloading the ZIP from GitHub and unzipping it) to install WoW Mentor.
# If macOS says it cannot be opened: right-click the file, choose Open, then Open again.
cd "$(dirname "$0")" || exit 1
chmod +x ./wowmentor.sh ./wowui
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "Python 3.9 or newer is not ready yet. macOS will now offer to install Apple's free Command Line Tools."
  echo "Click Install, wait for it to finish (a few minutes), then double-click this file again."
  xcode-select --install >/dev/null 2>&1 || true
  echo
  echo "Press Return to close this window."
  read -r _
  exit 1
fi
# The same short commands the one-line installer gives you: wowmentor and wowui.
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/wowmentor.sh" "$HOME/.local/bin/wowmentor"
ln -sf "$PWD/wowui" "$HOME/.local/bin/wowui"
./wowmentor.sh setup "$@"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    echo
    echo "Tip: to type 'wowmentor' in any Terminal window, add this line to a file called .zshrc in your home folder, then open a new Terminal window:"
    # shellcheck disable=SC2016  # the dollar signs are meant literally: this is text for the user to copy
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac
echo
echo "Do not delete or move this folder: WoW Mentor runs from here."
echo "Press Return to close this window."
read -r _
