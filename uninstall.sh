#!/bin/bash
# Removes Deckside: the background service, the deckside command and ~/.deckside.
#   --keep-settings   keep your token and buttons (~/.deckside/token.txt, deck.json) for a later reinstall
LABEL="com.deckside.server"
HOME_DIR="$HOME/.deckside"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
for d in /opt/homebrew/bin /usr/local/bin; do
  [ -L "$d/deckside" ] && rm -f "$d/deckside"
done
if [ -f "$HOME/.zshrc" ] && grep -q '.deckside/bin' "$HOME/.zshrc"; then
  sed -i '' -e '/^# Deckside$/d' -e '/\.deckside\/bin/d' "$HOME/.zshrc"
fi

if [ "${1:-}" = "--keep-settings" ]; then
  rm -rf "$HOME_DIR/app" "$HOME_DIR/bin" "$HOME_DIR/logs" "$HOME_DIR/uninstall.sh"
  echo "Deckside removed. Your settings are kept in $HOME_DIR."
else
  rm -rf "$HOME_DIR"
  echo "Deckside removed."
fi
echo "The Homebrew helpers stay installed; remove them with:  brew uninstall media-control switchaudio-osx qrencode"
