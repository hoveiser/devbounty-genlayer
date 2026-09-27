#!/usr/bin/env python
"""Render the DevBounty mark (frontend/assets/favicon.svg geometry) to rasters.

No SVG rasterizer is available on this box, so the identical geometry is
drawn with PIL at 2048px and Lanczos-downsampled:

  frontend/assets/favicon.svg        canonical vector (hand-written source)
  frontend/assets/favicon-{16,32,48}.png
  frontend/assets/favicon.ico        16+32+48 multi-size
  frontend/assets/apple-touch-icon.png  180px
  assets/logo.png                    512px transparent project logo

Re-run after any geometry change:  python3 scripts/gen_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "frontend" / "assets"
SS = 2048  # supersample canvas

BG = (15, 24, 41)          # #0F1829 tile
BG_STROKE = (36, 52, 79)   # #24344F border
BLUE = (91, 140, 255)      # #5B8CFF brackets
GOLD = (247, 178, 59)      # #F7B23B coin
GOLD_RIM = (180, 118, 26)  # #B4761A coin stroke
CORE = (138, 90, 16)       # #8A5A10 coin core


def s(v: float) -> float:
    """scale design-space (1024) coordinate to the supersample canvas"""
    return v * SS / 1024.0


def render(size: int, tile: bool = True) -> Image.Image:
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if tile:
        d.rounded_rectangle(
            [s(36), s(36), s(988), s(988)],
            radius=s(212), fill=BG, outline=BG_STROKE, width=round(s(34)),
        )
    w = round(s(76))
    for pts in ([(392, 326), (230, 512), (392, 698)], [(632, 326), (794, 512), (632, 698)]):
        p = [(s(x), s(y)) for x, y in pts]
        d.line(p, fill=BLUE, width=w, joint="curve")
        for (x, y) in (p[0], p[-1]):  # round end caps
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=BLUE)
    r, cx = s(124), s(512)
    d.ellipse([cx - r, cx - r, cx + r, cx + r], fill=GOLD, outline=GOLD_RIM, width=round(s(26)))
    rc = s(48)
    d.ellipse([cx - rc, cx - rc, cx + rc, cx + rc], fill=CORE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ROOT / "assets").mkdir(exist_ok=True)
    pngs = {}
    for size in (16, 32, 48, 180, 512):
        pngs[size] = render(size)
    for size in (16, 32, 48):
        pngs[size].save(ASSETS / f"favicon-{size}.png")
    pngs[180].save(ASSETS / "apple-touch-icon.png")
    pngs[512].save(ROOT / "assets" / "logo.png")
    pngs[32].save(ASSETS / "favicon.ico", format="ICO",
                   sizes=[(16, 16), (32, 32), (48, 48)])
    print("wrote:", ", ".join(p.name for p in sorted(ASSETS.glob("favicon*")))
          + ", apple-touch-icon.png, assets/logo.png")


if __name__ == "__main__":
    main()
