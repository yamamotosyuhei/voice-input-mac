#!/usr/bin/env bash
# VoiceInput installer — idempotent, safe to re-run.
# Installs dependencies, downloads the model, builds the .app, and registers auto-start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="VoiceInput"
APP_DEST="/Applications/${APP_NAME}.app"
BUNDLE_ID="app.voiceinput"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/${BUNDLE_ID}.plist"

cd "$SCRIPT_DIR"

say()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }

# ─── 1. Homebrew dependencies ────────────────────────────────────────────────
say "Checking Homebrew dependencies"
if ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew not found. Install from https://brew.sh and re-run."
  exit 1
fi
for pkg in ffmpeg whisper-cpp; do
  if brew list "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed"
  else
    say "Installing $pkg"
    brew install "$pkg"
  fi
done

# ─── 2. Python dependencies ──────────────────────────────────────────────────
say "Installing Python dependencies (rumps, pyobjc, py2app)"
python3 -m pip install --quiet --user --upgrade rumps pyobjc py2app

# ─── 3. Model ────────────────────────────────────────────────────────────────
say "Downloading Whisper model"
bash "$SCRIPT_DIR/download_model.sh"

# ─── 4. Config files (only if user hasn't created them) ──────────────────────
say "Preparing config files"
if [[ ! -f "$SCRIPT_DIR/vocab.txt" ]]; then
  cp "$SCRIPT_DIR/vocab.txt.example" "$SCRIPT_DIR/vocab.txt"
  ok "Created vocab.txt from example"
else
  ok "vocab.txt already exists — left untouched"
fi
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  ok "Created .env from example (Groq key is optional)"
else
  ok ".env already exists — left untouched"
fi

# ─── 5. Build .app ───────────────────────────────────────────────────────────
say "Building ${APP_NAME}.app with py2app"
if [[ -d "$SCRIPT_DIR/build" ]]; then mv "$SCRIPT_DIR/build" "$SCRIPT_DIR/build.old.$(date +%s)"; fi
if [[ -d "$SCRIPT_DIR/dist"  ]]; then mv "$SCRIPT_DIR/dist"  "$SCRIPT_DIR/dist.old.$(date +%s)";  fi
python3 setup.py py2app --alias --quiet

BUILT_APP="$SCRIPT_DIR/dist/${APP_NAME}.app"
if [[ ! -d "$BUILT_APP" ]]; then
  warn "Build failed — ${BUILT_APP} not found"
  exit 1
fi

# ─── 6. Install to /Applications ─────────────────────────────────────────────
say "Installing to ${APP_DEST}"
if [[ -d "$APP_DEST" ]]; then
  mv "$APP_DEST" "${APP_DEST}.old.$(date +%s)"
  warn "Existing app moved aside — you can delete the .old.* copy after verifying"
fi
cp -R "$BUILT_APP" "$APP_DEST"
ok "Installed at $APP_DEST"

# ─── 7. LaunchAgent for auto-start ───────────────────────────────────────────
say "Registering LaunchAgent (auto-start on login)"
mkdir -p "$LAUNCH_AGENT_DIR"
cat > "$LAUNCH_AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>${APP_DEST}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>/tmp/${BUNDLE_ID}.err</string>
    <key>StandardOutPath</key>
    <string>/tmp/${BUNDLE_ID}.out</string>
</dict>
</plist>
PLIST

# Reload if already loaded
launchctl bootout "gui/$(id -u)/${BUNDLE_ID}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PLIST"
ok "LaunchAgent registered: $LAUNCH_AGENT_PLIST"

# ─── 8. First launch ─────────────────────────────────────────────────────────
say "Launching ${APP_NAME}"
open -a "$APP_DEST"

cat <<DONE

────────────────────────────────────────────────────────────
✓ Install complete.

Look for 🎤 in your menubar. The first time you press Fn, macOS will ask for:
  1. Microphone permission   (needed to record)
  2. Input Monitoring        (needed to detect Fn globally)
  3. Accessibility           (needed to paste with ⌘V)

After granting Input Monitoring and Accessibility, quit VoiceInput from the
menubar and relaunch — those two only take effect after restart.

Try it: click into any text field, hold Fn, speak, release.

Config files (edit and restart the app to apply):
  - $SCRIPT_DIR/vocab.txt
  - $SCRIPT_DIR/.env

Quit anytime from menubar 🎤 → 終了.
Uninstall:  see README.md → Uninstall section.
────────────────────────────────────────────────────────────
DONE
