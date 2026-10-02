#!/usr/bin/env bash
# Install an ffmpeg that can decode Dolby AC-4 into ./bin.
#
# ATSC 3.0 broadcasts carry AC-4 audio, and stock ffmpeg (Ubuntu 6.1.1
# included) ships no AC-4 decoder, so those recordings extract to nothing and
# can never be transcribed. Recent ffmpeg git builds do decode it.
#
# The SageTV container already carries such a build for its own transcoding, so
# by default we copy that one out rather than compiling ffmpeg here. Point the
# script at any other AC-4-capable binary with:
#
#   FFMPEG_AC4_SOURCE=/path/to/ffmpeg scripts/install-ac4-ffmpeg.sh
#
# The binary is intentionally NOT committed: it is ~30 MB, platform-specific,
# and not ours to redistribute.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT/bin"
DEST="$DEST_DIR/ffmpeg-ac4"
CONTAINER="${SAGETV_CONTAINER:-sagetv-mine}"
CONTAINER_PATH="${SAGETV_FFMPEG_PATH:-/usr/local/bin/ffmpeg-ac4}"

mkdir -p "$DEST_DIR"

decodes_ac4() {
    # All entries listed by -decoders are decoders; match the name column so a
    # codec that merely mentions ac4 in its description cannot false-positive.
    #
    # The output is captured before grepping on purpose: piping straight into
    # "grep -q" makes grep exit on the first match, ffmpeg dies of SIGPIPE, and
    # under "set -o pipefail" that 141 becomes the function's exit status -- so
    # a perfectly good binary reports "no AC-4 decoder".
    local list
    list="$("$1" -hide_banner -decoders 2>/dev/null || true)"
    printf '%s\n' "$list" | grep -qE '^[[:space:]]*[^[:space:]]+[[:space:]]+ac4[[:space:]]'
}

if [[ -n "${FFMPEG_AC4_SOURCE:-}" ]]; then
    echo "==> Copying from $FFMPEG_AC4_SOURCE"
    cp "$FFMPEG_AC4_SOURCE" "$DEST"
elif docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "==> Copying $CONTAINER:$CONTAINER_PATH"
    docker cp "$CONTAINER:$CONTAINER_PATH" "$DEST"
    # ffprobe counterpart is optional; only the decoder matters for extraction.
    docker cp "$CONTAINER:${CONTAINER_PATH/ffmpeg/ffprobe}" "$DEST_DIR/ffprobe-ac4" 2>/dev/null \
        && chmod +x "$DEST_DIR/ffprobe-ac4" || true
else
    echo "ERROR: container '$CONTAINER' not found and FFMPEG_AC4_SOURCE unset." >&2
    echo "Supply an AC-4-capable ffmpeg, e.g. a recent build from" >&2
    echo "  https://github.com/BtbN/FFmpeg-Builds/releases" >&2
    exit 1
fi

chmod +x "$DEST"

# The binary is dynamically linked, so a copy from a container with a different
# base image can land here and fail only later, mid-job. Check now instead.
echo "==> Verifying"
if ! "$DEST" -version >/dev/null 2>&1; then
    echo "ERROR: $DEST will not run on this host." >&2
    ldd "$DEST" 2>&1 | grep -i "not found" >&2 || true
    rm -f "$DEST"
    exit 1
fi
if ! decodes_ac4 "$DEST"; then
    echo "ERROR: $DEST has no AC-4 decoder." >&2
    rm -f "$DEST"
    exit 1
fi

echo "==> OK: $("$DEST" -version | head -1)"
echo "==> AC-4 decoder present; transcription will use it automatically."
