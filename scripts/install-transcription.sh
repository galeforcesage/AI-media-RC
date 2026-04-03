#!/usr/bin/env bash
# install-transcription.sh — Set up the Transcription Subsystem
#
# Installs Python dependencies, configures speaker diarization (optional),
# and prepares the service for running.
#
# Usage:
#   cd /path/to/AI-media-RC
#   bash scripts/install-transcription.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TRANS_DIR="$ROOT_DIR/backend/transcription"

echo "=== Transcription Subsystem Installer ==="
echo ""

# ── 1. Python venv ────────────────────────────────────────────────────
echo "[1/5] Setting up Python virtual environment..."
cd "$TRANS_DIR"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "  Created .venv"
else
    echo "  .venv already exists"
fi

source .venv/bin/activate

# ── 2. Core dependencies ─────────────────────────────────────────────
echo "[2/5] Installing core dependencies..."
pip install --upgrade pip -q
pip install faster-whisper aiohttp -q
echo "  Core dependencies installed"

# ── 3. ffmpeg check ──────────────────────────────────────────────────
echo "[3/5] Checking ffmpeg..."
if command -v ffmpeg &>/dev/null; then
    echo "  ffmpeg found: $(ffmpeg -version 2>&1 | head -1)"
else
    echo "  WARNING: ffmpeg not found. Install it:"
    echo "    sudo apt install ffmpeg   # Debian/Ubuntu"
    echo "    sudo yum install ffmpeg   # RHEL/CentOS"
fi

# ── 4. Speaker diarization (optional) ────────────────────────────────
echo ""
echo "[4/5] Speaker Diarization Setup (optional)"
echo ""
echo "  Speaker diarization identifies WHO is speaking in transcripts and"
echo "  maps speakers to character names from your media metadata."
echo ""
echo "  It requires:"
echo "    a) The pyannote.audio Python package (installs ~500MB of PyTorch)"
echo "    b) A free HuggingFace account token"
echo "    c) Accepting the gated model license agreements"
echo ""
read -rp "  Install speaker diarization? [y/N] " INSTALL_DIARIZATION

if [[ "${INSTALL_DIARIZATION,,}" == "y" ]]; then
    echo ""
    echo "  Installing pyannote.audio (this may take a few minutes)..."
    pip install pyannote.audio -q
    echo "  pyannote.audio installed."
    echo ""

    # Check for existing token
    if [ -f "$HOME/.huggingface/token" ]; then
        echo "  HuggingFace token already configured."
    else
        echo "  ── HuggingFace Token Setup ──"
        echo ""
        echo "  Before entering your token, you MUST accept the model licenses:"
        echo ""
        echo "    1. Create a free account at https://huggingface.co/join"
        echo "    2. Create a READ-only token at https://huggingface.co/settings/tokens"
        echo "    3. Accept the license at https://huggingface.co/pyannote/speaker-diarization-3.1"
        echo "       (click 'Agree and access repository')"
        echo "    4. Accept the license at https://huggingface.co/pyannote/segmentation-3.0"
        echo "       (click 'Agree and access repository')"
        echo ""
        read -rp "  Have you completed steps 1-4 above? [y/N] " ACCEPTED
        if [[ "${ACCEPTED,,}" == "y" ]]; then
            read -rsp "  Paste your HuggingFace token (input hidden): " HF_TOKEN_INPUT
            echo ""
            if [ -n "$HF_TOKEN_INPUT" ]; then
                mkdir -p "$HOME/.huggingface"
                chmod 700 "$HOME/.huggingface"
                echo "$HF_TOKEN_INPUT" > "$HOME/.huggingface/token"
                chmod 600 "$HOME/.huggingface/token"
                echo "  Token saved to ~/.huggingface/token (permissions: 600)"
            else
                echo "  No token entered. Diarization will be disabled at runtime."
                echo "  You can add it later: echo 'hf_YOUR_TOKEN' > ~/.huggingface/token"
            fi
        else
            echo "  Please complete those steps and re-run, or add the token later:"
            echo "    echo 'hf_YOUR_TOKEN' > ~/.huggingface/token && chmod 600 ~/.huggingface/token"
        fi
    fi

    echo ""
    echo "  The diarization model (~100MB) will download on first transcription."
else
    echo "  Skipping diarization. Transcripts will not have speaker labels."
    echo "  You can install it later by re-running this script."
fi

# ── 5. Summary ───────────────────────────────────────────────────────
echo ""
echo "[5/5] Installation complete!"
echo ""
echo "  Start the service:"
echo "    cd $TRANS_DIR"
echo "    PYTHONPATH=. .venv/bin/python3 main.py --port 8770 \\"
echo "      --sagetv-mcp 127.0.0.1:8766 \\"
echo "      --channels-mcp 127.0.0.1:8767"
echo ""
echo "  Options:"
echo "    --no-live       Disable live transcription during recording"
echo "    --no-watchers   Disable file watchers (server-only mode)"
echo "    --no-worker     Disable worker (server-only mode)"
echo "    --whisper-model auto|base|small|medium|large-v3"
echo "    --debug         Enable debug logging"
echo ""
