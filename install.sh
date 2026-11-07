#!/bin/bash
# Deckside installer - turns a spare iPad or phone into a control panel and deck for this Mac.
#
#   curl -fsSL https://raw.githubusercontent.com/aalharthi977/deckside/main/install.sh | bash
#
# Safe to run again: it updates the app and keeps your token, buttons and settings.
# Options (environment):  DECKSIDE_PORT=8765   DECKSIDE_REPO=aalharthi977/deckside   DECKSIDE_REF=main
#                         DECKSIDE_NO_BREW=1 (skip Homebrew helpers)
set -euo pipefail

REPO="${DECKSIDE_REPO:-aalharthi977/deckside}"
REF="${DECKSIDE_REF:-main}"
PORT="${DECKSIDE_PORT:-8765}"
HOME_DIR="$HOME/.deckside"            # outside Documents/Desktop/Downloads, so macOS privacy rules don't block it
APP_DIR="$HOME_DIR/app"
LABEL="com.deckside.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
# Questions read the keyboard directly, so they work when this script is piped from curl.
ask()  { local a=""; if [ -r /dev/tty ]; then printf '%s ' "$1" > /dev/tty; read -r a < /dev/tty || a=""; fi; printf '%s' "$a"; }

[ "$(uname -s)" = "Darwin" ] || die "Deckside runs on macOS only."

bold ""
bold "  Deckside · control your Mac from a spare iPad or phone"
bold ""

# ------------------------------------------------------------------ port
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1 && ! curl -fs "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q deckside; then
  die "Port ${PORT} is already used by another app. Install on a different port instead:
    curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | DECKSIDE_PORT=8766 bash"
fi

# ------------------------------------------------------------------ python
step "Checking Python"
PY=""
if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ]; then
  PY=/usr/bin/python3        # Apple's copy: its path never changes, so macOS permissions stick
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
  PY="$(command -v python3)"
fi
if [ -z "$PY" ]; then
  note "Python comes with Apple's Command Line Tools. A window will open to install them;"
  note "when it finishes, run this installer again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi
note "Using $PY"

# ------------------------------------------------------------------ homebrew helpers
BREW=""
for b in "$(command -v brew 2>/dev/null || true)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -n "$b" ] && [ -x "$b" ] && { BREW="$b"; break; }
done
if [ -z "${DECKSIDE_NO_BREW:-}" ]; then
  step "Checking helpers (Homebrew)"
  if [ -z "$BREW" ]; then
    note "Homebrew installs three small helpers: now playing + play/pause in any app,"
    note "sound device switching, and the pairing QR code. Without it those are skipped."
    a="$(ask '    Install Homebrew now? [Y/n]')"
    case "$a" in
      n|N|no|NO) note "Skipping. You can run this installer again later." ;;
      *) /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
         for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { BREW="$b"; break; }; done ;;
    esac
  fi
  if [ -n "$BREW" ]; then
    for f in media-control switchaudio-osx qrencode; do
      if "$BREW" list --formula "$f" >/dev/null 2>&1; then
        note "$f ✓"
      else
        note "Installing ${f}..."
        "$BREW" install --quiet "$f" >/dev/null 2>&1 || note "Couldn't install $f (Deckside still works; that feature is skipped)."
      fi
    done
  fi
fi

# ------------------------------------------------------------------ app files
step "Installing Deckside"
SRC=""
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then   # empty when piped from curl
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$HERE" ] && [ -f "$HERE/app/server.py" ]; then
  SRC="$HERE"                               # run from a downloaded copy of the repo
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  note "Downloading ${REPO} (${REF})..."
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" \
    || die "Download failed. Check your internet connection, or download the repo and run ./install.sh from it."
  SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -f "$SRC/app/server.py" ] || die "The download doesn't look like Deckside."
fi
mkdir -p "$HOME_DIR/logs" "$HOME_DIR/bin"
rm -rf "$APP_DIR.new"; cp -R "$SRC/app" "$APP_DIR.new"
rm -rf "$APP_DIR"; mv "$APP_DIR.new" "$APP_DIR"
cp "$SRC/deckside" "$HOME_DIR/bin/deckside"; chmod 755 "$HOME_DIR/bin/deckside"
cp "$SRC/uninstall.sh" "$HOME_DIR/uninstall.sh"; chmod 755 "$HOME_DIR/uninstall.sh"
if [ ! -s "$HOME_DIR/token.txt" ]; then
  ( umask 077; "$PY" -c 'import secrets; print(secrets.token_urlsafe(12))' > "$HOME_DIR/token.txt" )
fi
printf '%s\n' "$REPO" > "$HOME_DIR/repo"
note "App: $APP_DIR"

# `deckside` command on the PATH
LINKED=""
for d in /opt/homebrew/bin /usr/local/bin; do
  if [ -d "$d" ] && [ -w "$d" ]; then ln -sf "$HOME_DIR/bin/deckside" "$d/deckside"; LINKED="$d"; break; fi
done
if [ -z "$LINKED" ]; then
  RC="$HOME/.zshrc"; LINE='export PATH="$HOME/.deckside/bin:$PATH"'
  grep -qsF "$LINE" "$RC" || printf '\n# Deckside\n%s\n' "$LINE" >> "$RC"
  note "Added ~/.deckside/bin to your PATH (open a new Terminal window to use the deckside command)."
fi

# ------------------------------------------------------------------ login service
step "Starting the background service"
mkdir -p "$HOME/Library/LaunchAgents"
ORIGIN_XML=""
[ -n "${DECKSIDE_ALLOW_ORIGIN:-}" ] && ORIGIN_XML="<key>DECKSIDE_ALLOW_ORIGIN</key><string>$DECKSIDE_ALLOW_ORIGIN</string>"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$APP_DIR/server.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DECKSIDE_PORT</key><string>$PORT</string>
    <key>DECKSIDE_DATA</key><string>$HOME_DIR</string>
    $ORIGIN_XML
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/server.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/server.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf '%s\n' "$PORT" > "$HOME_DIR/port"

ok=""
for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q deckside; then ok=1; break; fi
  sleep 0.5
done
[ -n "$ok" ] || die "Deckside didn't start. See the log:  tail -n 40 $HOME_DIR/logs/server.log"
note "Running on port $PORT, starts automatically when you log in."

# ------------------------------------------------------------------ pair
"$HOME_DIR/bin/deckside" pair

cat <<EOF

  $(bold "Two one-time permissions")
   • The first time you tap a keyboard-shortcut button, macOS asks to let python3
     control "System Events". Click Allow.
   • If macOS asks whether python3 may accept incoming network connections, click Allow.

  Commands:  deckside pair · deckside status · deckside restart · deckside uninstall

EOF
