#!/usr/bin/env python3
"""Generate PNG icons from SVG for PWA manifest.
Requires: pip install cairosvg  (or: apt install python3-cairosvg)
Fallback: uses rsvg-convert if available.
"""
import subprocess, sys, os

ICONS_DIR = os.path.dirname(os.path.abspath(__file__))

TARGETS = [
    ("icon.svg",          "icon-192.png",          192),
    ("icon.svg",          "icon-512.png",          512),
    ("icon-maskable.svg", "icon-maskable-192.png", 192),
    ("icon-maskable.svg", "icon-maskable-512.png", 512),
]

def try_cairosvg():
    try:
        import cairosvg
        for src, dst, size in TARGETS:
            cairosvg.svg2png(
                url=os.path.join(ICONS_DIR, src),
                write_to=os.path.join(ICONS_DIR, dst),
                output_width=size, output_height=size
            )
            print(f"  {dst} ({size}x{size})")
        return True
    except ImportError:
        return False

def try_rsvg():
    for src, dst, size in TARGETS:
        r = subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size),
             os.path.join(ICONS_DIR, src), "-o", os.path.join(ICONS_DIR, dst)],
            capture_output=True
        )
        if r.returncode != 0:
            return False
        print(f"  {dst} ({size}x{size})")
    return True

def try_inkscape():
    for src, dst, size in TARGETS:
        r = subprocess.run(
            ["inkscape", os.path.join(ICONS_DIR, src),
             "-w", str(size), "-h", str(size),
             "-o", os.path.join(ICONS_DIR, dst)],
            capture_output=True
        )
        if r.returncode != 0:
            return False
        print(f"  {dst} ({size}x{size})")
    return True

print("Generating PWA icons...")
if try_cairosvg():
    print("Done (cairosvg)")
elif try_rsvg():
    print("Done (rsvg-convert)")
elif try_inkscape():
    print("Done (inkscape)")
else:
    print("ERROR: No SVG converter found.")
    print("Install one of: pip install cairosvg | apt install librsvg2-bin | apt install inkscape")
    sys.exit(1)
