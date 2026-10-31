"""Draw the Scorn app icon and write src/scorn/static/scorn.ico.

    uv run --offline python scripts/make_icon.py [--preview out.png]

A sheet of paper with a beamed pair of eighth notes lifting off it toward the
upper right, in the page accent blue. Sizes 32 px and up are drawn as vectors
(supersampled, then downscaled); 16 and 24 px are hand-placed pixel grids so
they stay legible instead of blurring into a smudge.
"""

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = ROOT / "src" / "scorn" / "static" / "scorn.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BLUE = (0x24, 0x57, 0xC5, 255)
PAPER = (255, 255, 255, 255)
OUTLINE = (0x5D, 0x6B, 0x82, 255)
FOLD = (0xD9, 0xE0, 0xEC, 255)
STAFF = (0xBF, 0xC9, 0xDB, 255)

PALETTE = {".": (0, 0, 0, 0), "o": OUTLINE, "w": PAPER, "f": FOLD, "b": BLUE, "s": STAFF}

GRID_16 = [
    "................",
    "..........bbbbb.",
    ".....bbbbbbbbbb.",
    ".....bbbbb....b.",
    "ooooobo.......b.",
    "owwwwboo......b.",
    "owwwwbofo.....b.",
    "owwwwboooo....b.",
    "owwwwbwwwo....b.",
    "owwwwbwwwo....b.",
    "owwwwbwwwo..bbb.",
    "owwwwbwwwo.bbbb.",
    "owwbbbwwwo.bbb..",
    "owbbbbwwwo......",
    "owbbbwwwwo......",
    "oooooooooo......",
]

GRID_24 = [
    "........................",
    "........................",
    "...............bbbbbbb..",
    "........bbbbbbbbbbbbbb..",
    "........bbbbbbbbbbbbbb..",
    "........bbbbbbbb....bb..",
    "........bb..........bb..",
    "ooooooo.bb.oo.......bb..",
    "owwwwwo.bb.owo......bb..",
    "owwwwwwwbbwofo......bb..",
    "owwwwwwwbbwoffo.....bb..",
    "owwwwwwwbbwooooo....bb..",
    "owwwwwwwbbwwwwwo....bb..",
    "owwwwwwwbbwwwwwo....bb..",
    "owwwwwwwbbwwwwwo..bbbb..",
    "owwwwwwwbbwwwwwo.bbbbb..",
    "owwwwwwwbbwwwwwo.bbbbb..",
    "owwwwbbbbbwwwwwo..bbb...",
    "owwwbbbbbbwwwwwo........",
    "owwwbbbbbbwwwwwo........",
    "owwwwbbbwwwwwwwo........",
    "owwwwwwwwwwwwwwo........",
    "oooooooooooooooo........",
    "........................",
]


def from_grid(grid: list[str]) -> Image.Image:
    size = len(grid)
    im = Image.new("RGBA", (size, size))
    for y, row in enumerate(grid):
        assert len(row) == size, (size, y)
        for x, ch in enumerate(row):
            im.putpixel((x, y), PALETTE[ch])
    return im


def ellipse_points(cx: float, cy: float, rx: float, ry: float, deg: float, n: int = 72) -> list[tuple[float, float]]:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def draw_notes(d: ImageDraw.ImageDraw, k: float, grow: float, color: tuple[int, int, int, int]) -> None:
    """Beamed eighth pair in 256-unit coordinates; grow > 0 draws the halo."""

    def s(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * k, y * k) for x, y in pts]

    g = grow
    for cx, cy in ((62, 206), (194, 166)):
        d.polygon(s(ellipse_points(cx, cy, 31 + g, 22 + g, -25)), fill=color)
    # Stems sit on the right edge of each head; the beam spans their outer edges.
    for x0, y0, y1 in ((80, 36, 204), (212, 12, 164)):
        d.polygon(s([(x0 - g, y0 - g), (x0 + 13 + g, y0 - g), (x0 + 13 + g, y1), (x0 - g, y1)]), fill=color)
    d.polygon(s([(80 - g, 36 - g), (225 + g, 12 - g), (225 + g, 38 + g), (80 - g, 62 + g)]), fill=color)


def vector(size: int) -> Image.Image:
    ss = 4
    n = size * ss
    k = n / 256
    im = Image.new("RGBA", (n, n))
    d = ImageDraw.Draw(im)

    def s(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * k, y * k) for x, y in pts]

    # Page edges on multiples of 8 land on whole pixels at 32 px, keeping the outline crisp.
    w = 8
    page = [(8, 64), (112, 64), (160, 112), (160, 248), (8, 248)]
    inner = [(8 + w, 64 + w), (112 - 3, 64 + w), (160 - w, 112 + 3), (160 - w, 248 - w), (8 + w, 248 - w)]
    d.polygon(s(page), fill=OUTLINE)
    d.polygon(s(inner), fill=PAPER)
    flap = [(112, 64), (112, 112), (160, 112)]
    d.polygon(s(flap), fill=OUTLINE)
    d.polygon(s([(112 + w, 64 + 2.4 * w), (112 + w, 112 - w), (160 - 2.4 * w, 112 - w)]), fill=FOLD)

    # Staff lines thin out below 48 px, where they would only read as gray mush.
    if size >= 48:
        for i in range(5):
            y = 132 + 22 * i
            d.rectangle(s([(26, y - 3.5), (142, y + 3.5)]), fill=STAFF)

    draw_notes(d, k, 9, PAPER)
    draw_notes(d, k, 0, BLUE)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def render(size: int) -> Image.Image:
    if size == 16:
        return from_grid(GRID_16)
    if size == 24:
        return from_grid(GRID_24)
    return vector(size)


def preview(images: dict[int, Image.Image], path: Path) -> None:
    pad = 16
    big = {s: im.resize((s * max(1, 256 // s),) * 2, Image.Resampling.NEAREST) for s, im in images.items()}
    width = pad + sum(b.width + pad for b in big.values())
    band_h = pad + 256 + pad + 256 + pad
    sheet = Image.new("RGBA", (width, 2 * band_h))
    d = ImageDraw.Draw(sheet)
    for band, bg in enumerate([(0xF4, 0xF5, 0xF7, 255), (0x1E, 0x1F, 0x22, 255)]):
        top = band * band_h
        d.rectangle([0, top, width, top + band_h], fill=bg)
        x = pad
        for s in SIZES:
            sheet.alpha_composite(images[s], (x, top + pad))
            sheet.alpha_composite(big[s], (x, top + 2 * pad + 256))
            x += big[s].width + pad
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", type=Path, help="also write a preview sheet PNG here")
    args = ap.parse_args()

    images = {s: render(s) for s in SIZES}
    largest = images[SIZES[-1]]
    others = [images[s] for s in SIZES[:-1]]
    # Without append_images Pillow downscales the largest frame for every size.
    largest.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in SIZES], append_images=others)
    print(f"wrote {ICO_PATH.relative_to(ROOT)}")
    if args.preview:
        preview(images, args.preview)
        print(f"wrote {args.preview}")


if __name__ == "__main__":
    main()
