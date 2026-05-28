#!/usr/bin/env bash
# setup-auth.sh — Configure authentication for the LLM Remote system.
#
# Usage:  sudo ./scripts/setup-auth.sh
#
# This script:
#   1. Installs the sudoers config for passwordless sudo on allowlisted commands
#   2. Generates auth.json with hashed passwords (interactive prompts)
#
# Must be run as root (or via sudo).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SUDOERS_SRC="${PROJECT_DIR}/deploy/sudoers/ai-media-rc"
SUDOERS_DST="/etc/sudoers.d/ai-media-rc"
AUTH_JSON="${PROJECT_DIR}/backend/session-manager/auth.json"

# ── Check root ───────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (try: sudo $0)"
    exit 1
fi

echo "=== LLM Remote — Auth Setup ==="
echo

# ── 1. Install sudoers ──────────────────────────────────
echo "[1/2] Installing sudoers config..."
if [[ -f "$SUDOERS_SRC" ]]; then
    # Validate syntax before installing
    if visudo -c -f "$SUDOERS_SRC" 2>/dev/null; then
        cp "$SUDOERS_SRC" "$SUDOERS_DST"
        chmod 0440 "$SUDOERS_DST"
        echo "  ✓ Installed $SUDOERS_DST"
    else
        echo "  ✗ Sudoers file has syntax errors — not installed!"
        exit 1
    fi
else
    echo "  ✗ Source file not found: $SUDOERS_SRC"
    exit 1
fi

# ── 2. Generate auth.json ───────────────────────────────
echo
echo "[2/2] Configure authentication passwords..."
echo

# Determine which python to use
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "  ✗ Python not found"
    exit 1
fi

# Prompt for passwords
read -rsp "App password (shared access): " APP_PASS
echo
if [[ -z "$APP_PASS" ]]; then
    echo "App password cannot be empty"
    exit 1
fi

read -rp  "Admin username (default: user): " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-user}"

read -rsp "Admin password: " ADMIN_PASS
echo
if [[ -z "$ADMIN_PASS" ]]; then
    echo "Admin password cannot be empty"
    exit 1
fi

# Generate auth.json via Python (reuses the auth module's hashing)
$PYTHON -c "
import sys, os
sys.path.insert(0, os.path.join('${PROJECT_DIR}', 'backend', 'session-manager', 'src'))
from auth import hash_password
import json, secrets

data = {
    'secret_key': secrets.token_hex(32),
    'app_password_hash': hash_password('${APP_PASS}'),
    'admin_users': {
        '${ADMIN_USER}': hash_password('${ADMIN_PASS}'),
    },
}
with open('${AUTH_JSON}', 'w') as f:
    json.dump(data, f, indent=2)
os.chmod('${AUTH_JSON}', 0o600)
print(f'  ✓ Auth config written to ${AUTH_JSON}')
"

# Fix ownership (script runs as root, file should be owned by sagetv)
OWNER="${SUDO_USER:-sagetv}"
chown "$OWNER:$OWNER" "$AUTH_JSON" 2>/dev/null || true

echo
echo "=== Setup Complete ==="
echo "  - Sudoers: $SUDOERS_DST"
echo "  - Auth:    $AUTH_JSON"
echo "  - Restart session-manager to pick up changes."
echo
