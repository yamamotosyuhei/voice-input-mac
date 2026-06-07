#!/usr/bin/env bash
# Downloads the Whisper model from HuggingFace.
# Default: large-v3-turbo q5 (~512 MB) — fast and accurate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/models"
MODEL_NAME="${MODEL_NAME:-ggml-large-v3-turbo-q5_0.bin}"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL_NAME"

mkdir -p "$MODELS_DIR"
DEST="$MODELS_DIR/$MODEL_NAME"

if [[ -f "$DEST" ]]; then
  size=$(stat -f%z "$DEST" 2>/dev/null || stat -c%s "$DEST")
  if (( size > 100000000 )); then
    echo "✓ Model already exists: $DEST ($(( size / 1024 / 1024 )) MB)"
    exit 0
  else
    echo "⚠ Existing model looks truncated ($size bytes). Re-downloading…"
    mv "$DEST" "$DEST.partial.$(date +%s)"
  fi
fi

echo "↓ Downloading $MODEL_NAME (~512 MB)…"
curl -L --fail --progress-bar -o "$DEST" "$URL"

echo "✓ Model saved to $DEST"
