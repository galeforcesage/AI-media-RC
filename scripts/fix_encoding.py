#!/usr/bin/env python3
"""Fix double-encoded UTF-8 in index.html (mojibake)."""
import sys, re

path = sys.argv[1] if len(sys.argv) > 1 else "frontend/index.html"

with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# Strip BOM if present
if text.startswith("\ufeff"):
    text = text[1:]

# Map of mojibake sequences → correct Unicode chars
# These are UTF-8 bytes misinterpreted as Latin-1 then re-encoded as UTF-8
replacements = {
    "ðŸŽ¤": "🎤",     # U+1F3A4 MICROPHONE
    "ðŸ›\xa0": "🛠",    # U+1F6E0 HAMMER AND WRENCH
    "ðŸšª": "🚪",     # U+1F6AA DOOR
    "â–¾": "▾",       # U+25BE
    "â–²": "▲",       # U+25B2
    "â–¶": "▶",       # U+25B6
    "â–¼": "▼",       # U+25BC
    "â—€": "◀",       # U+25C0
    "âž¤": "➤",       # U+27A4
    "âš™": "⚙",       # U+2699
    "â†©": "↩",       # U+21A9
    "âœ•": "✕",       # U+2715
    "â˜°": "☰",       # U+2630
}

# Handle 3-byte sequences that include control chars (U+008F etc.)
# ⏪ = E2 8F AA → â\x8Fª when double-encoded
replacements["â\x8fª"] = "⏪"   # U+23EA
replacements["â\x8f¹"] = "⏹"   # U+23F9
replacements["â\x8f©"] = "⏩"   # U+23E9
replacements["â\x8f­"] = "⏭"   # U+23ED
replacements["â\x8fº"] = "⏺"   # U+23FA

count = 0
for bad, good in replacements.items():
    n = text.count(bad)
    if n:
        text = text.replace(bad, good)
        count += n
        print(f"  Replaced {n}x: {repr(bad)} -> {good}")

with open(path, "w", encoding="utf-8") as f:
    f.write(text)

print(f"Fixed {count} replacements in {path}")

# Verify
for line in text.splitlines():
    if "btn-voice" in line or "btn-send" in line or "footer-btn" in line:
        print(f"  {line.strip()}")
