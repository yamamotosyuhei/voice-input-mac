# VoiceInput for macOS

Menubar voice-to-text for macOS. Hold **Fn** (Globe key) to record, release to paste at the cursor — like AquaVoice, but local-first, faster, and free.

- **~0.5s** transcription with local [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (offline)
- **Push-to-talk** (hold Fn) or **hands-free** (double-tap Fn → speak → auto-stop on silence)
- **Vocabulary tuning** — add proper nouns to `vocab.txt`, no more mishearing your team's names
- **Voice commands** — say 改行 / まる / 取り消し to insert newline / period / undo
- **Optional Groq fallback** for when local isn't ready
- **Japanese polishing** (optional) via Groq LLM — strips fillers, fixes particles

> Originally built as a personal AquaVoice replacement. Tuned for Japanese but works with any Whisper language.

---

## Requirements

- macOS (Apple Silicon recommended — tested on M5)
- Python 3.9+
- [Homebrew](https://brew.sh) for installing dependencies
- About **600 MB** of disk for the Whisper model

---

## Install

```bash
git clone https://github.com/yamamotosyuhei/voice-input-mac.git
cd voice-input-mac
./install.sh
```

`install.sh` will:
1. Install `ffmpeg` and `whisper-cpp` via Homebrew (if missing)
2. Install Python deps (`rumps`, `pyobjc`, `py2app`)
3. Download the Whisper model (`ggml-large-v3-turbo-q5_0`, ~512 MB) to `models/`
4. Copy `vocab.txt.example` → `vocab.txt` and `.env.example` → `.env` if you don't have them yet
5. Build the `.app` bundle with `py2app` and copy it to `/Applications/VoiceInput.app`
6. Register a LaunchAgent so it auto-starts on login

### First-launch permissions (one-time)

macOS will ask for permissions the first time you press Fn:

1. **Microphone** — for recording
2. **Input Monitoring** — to detect Fn key presses globally
3. **Accessibility** — to paste with ⌘V

After granting Input Monitoring and Accessibility, restart the app for them to take effect.

---

## Usage

After install, look for **🎤** in the menubar.

| Action | Result |
|:--|:--|
| **Hold Fn** while speaking | Records while held → transcribes on release → pastes at cursor |
| **Double-tap Fn**, then speak | Hands-free mode → auto-stops after ~1.3 s of silence |
| Press Fn again during hands-free | Stop manually |

### Voice commands

Say one of these **alone** (no other words) and release Fn:

| Say | Effect |
|:--|:--|
| 改行 / かいぎょう / 新しい行 | Insert newline |
| まる / 句点 | Insert 。 |
| てん / 読点 | Insert 、 |
| 取り消し / 今の消して / アンドゥ | ⌘Z (undo last) |

Add more in `voice_input.py` — see `COMMANDS`.

### Vocabulary

Edit `~/Library/Application Support/VoiceInput/vocab.txt`, one word per line. After editing, restart VoiceInput (menubar → 終了 → relaunch). Whisper will favor these spellings.

```
# vocab.txt
Anthropic
whisper.cpp
Groq
TonyStark
```

### Switching engines

Click the menubar **🎤** → "エンジン" to toggle between **ローカル (offline, default)** and **Groq (cloud)**.

### Japanese polishing (optional)

Click **🎤** → "日本語校正: OFF" to turn ON. Adds ~0.5 s and requires a Groq API key. Removes filler words and fixes particles. Doesn't add or remove information.

---

## Configuration

Edit constants at the top of `voice_input.py`:

| Setting | Default | What it does |
|:--|:--|:--|
| `DEFAULT_ENGINE` | `"local"` | `"local"` (offline) or `"groq"` (cloud) |
| `LOCAL_PORT` | `8765` | Port for the local whisper-server |
| `LANGUAGE` | `"ja"` | Whisper language code (`"en"`, `"es"`, etc.) |
| `DOUBLE_CLICK_SEC` | `0.4` | Threshold for hold vs double-tap |
| `SILENCE_STOP_SEC` | `1.3` | Hands-free auto-stop after this much silence |
| `VOICE_RMS` | `500` | Audio level threshold for "speech detected" |
| `PASTE_AFTER` | `True` | Auto-paste after transcription |

After editing, copy the file into the bundle and relaunch (see **Updating** below).

### Optional: Groq API key

If you want Groq fallback or Japanese polishing, get a key at [console.groq.com/keys](https://console.groq.com/keys) and put it in `~/Library/Application Support/VoiceInput/.env`:

```
GROQ_API_KEY=gsk_...
```

### Config locations

| Path | Purpose |
|:--|:--|
| `~/Library/Application Support/VoiceInput/vocab.txt` | Proper-noun vocabulary list |
| `~/Library/Application Support/VoiceInput/.env` | `GROQ_API_KEY=...` (optional) |
| `~/Library/Application Support/VoiceInput/models/` | Whisper model file |
| `~/Library/Application Support/VoiceInput/voice_input.log` | Runtime log |

Override the location with `VOICEINPUT_CONFIG_DIR=/path/to/dir` if you want.

---

## Updating the code

To update without losing macOS permissions (which are tied to the app bundle's code signature):

```bash
cp voice_input.py /Applications/VoiceInput.app/Contents/Resources/voice_input.py
# Then quit VoiceInput from the menubar and relaunch — permissions preserved.
```

Full rebuild (only when you change dependencies):

```bash
python3 setup.py py2app
# This re-signs the bundle, so macOS will ask for Mic / Input Monitoring / Accessibility again.
```

---

## Uninstall

```bash
# Stop and unregister auto-start
launchctl bootout gui/$(id -u)/app.voiceinput 2>/dev/null
rm ~/Library/LaunchAgents/app.voiceinput.plist

# Remove the app
rm -rf "/Applications/VoiceInput.app"

# (Optional) revoke permissions in System Settings → Privacy & Security
```

---

## Architecture

```
Fn key (Quartz CGEventTap)
  ↓
ffmpeg --avfoundation → ring buffer (always-on, no startup lag)
  ↓
On release/auto-stop: slice ring buffer → WAV
  ↓
Local whisper.cpp server (port 8765, model resident in RAM)   ← default
  ↓ on failure
Groq cloud (https keep-alive)                                 ← fallback
  ↓
Optional: Groq LLM polishing
  ↓
NSPasteboard → ⌘V (Quartz CGEventPost)
```

Key design choices:
- **Always-on microphone** in a ring buffer eliminates the ~0.4 s ffmpeg startup delay
- **whisper-server with model resident** in RAM avoids the model load on every utterance
- **Same Bundle ID across updates** so macOS keeps the granted permissions

---

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [ggerganov/whisper.cpp](https://github.com/ggerganov/whisper.cpp) — the local inference engine
- [Groq](https://groq.com) — fast cloud Whisper for fallback
- [rumps](https://github.com/jaredks/rumps) — Python menubar apps
- [py2app](https://py2app.readthedocs.io/) — Python → macOS .app bundling
