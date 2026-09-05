#!/usr/bin/env python3
"""Turn a single square-ish source image into Kairos's installable icon set.

    ./packaging/make-icons.py path/to/source.png

Run this whenever the app icon changes. It writes

    data/icons/hicolor/<size>x<size>/apps/org.kairos.Calendar.png

one file per size a desktop asks for, and nothing else, so the result is
entirely reproducible from the source image plus this file.

WHAT IT DOES, AND WHY

A picture of an icon is not an icon. Three things have to be fixed first:

1. **The background must go.** Source art usually sits on a page or a
   swatch. Left alone, that becomes an opaque rectangle behind the icon,
   which looks broken on a dark panel. We crop to the artwork and make
   everything outside its rounded-rectangle silhouette transparent.

2. **It must be square.** Icon themes assume it. Source art rarely is.

3. **It needs several sizes.** Downscaling a 512px image to 16px in the
   toolkit gives a smudge; downscaling deliberately, one step at a time,
   keeps the shape readable.

The crop is measured from the image rather than hard-coded, so a redraw at a
different size still works: we find the coloured field, note how far the
border extends beyond it, and cut there.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("This script needs Pillow and numpy:  pip install pillow numpy")

PROJECT = Path(__file__).resolve().parent.parent
ICON_DIR = PROJECT / "data" / "icons"
APP_ID = "org.kairos.Calendar"

#: The sizes a Linux desktop actually looks for.
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

#: The mask is built at this size and downsampled, which is what gives the
#: rounded corners a clean anti-aliased edge instead of a stair-step.
SUPERSAMPLE = 4

#: How much of the border to keep outside the coloured field. Slightly less
#: than the measured thickness: trimming a hair off a white edge is invisible,
#: whereas leaving a sliver of the original background is not.
BORDER_SAFETY = 3


def find_field(pixels: np.ndarray) -> tuple[int, int, int, int]:
    """Bounding box of the icon's coloured field, ignoring the background.

    "Coloured" means saturated — a pixel whose channels differ noticeably.
    That separates the artwork from both a white border and a near-white
    page, neither of which is saturated, without hard-coding a hue.
    """
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    brightness = pixels.mean(axis=2)
    field = (spread > 40) & (brightness < 210)

    # One erosion pass, to ignore stray speckles in a textured background.
    eroded = field.copy()
    for shift in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        eroded &= np.roll(field, shift, axis=(0, 1))
    if not eroded.any():
        eroded = field
    if not eroded.any():
        raise SystemExit("Could not find any coloured artwork in that image.")

    ys, xs = np.nonzero(eroded)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def measure_corner_radius(pixels: np.ndarray, box: tuple[int, int, int, int]) -> int:
    """Corner radius of the field, from how short its topmost row is.

    A rounded rectangle's top row spans ``width - 2 * radius``, so the radius
    falls straight out of measuring that run.
    """
    x0, y0, x1, y1 = box
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    brightness = pixels.mean(axis=2)
    field = (spread > 40) & (brightness < 210)

    width = x1 - x0 + 1
    radii = []
    for offset in range(3):
        row = np.nonzero(field[y0 + offset, x0:x1 + 1])[0]
        if len(row):
            radii.append((width - (row.max() - row.min() + 1)) / 2 - offset)
    return int(round(sorted(radii)[len(radii) // 2])) if radii else int(width * 0.18)


def page_colour(pixels: np.ndarray) -> np.ndarray:
    """The colour of whatever the artwork is sitting on.

    Sampled from the four corners of the image, which are the pixels least
    likely to be part of the icon. The median shrugs off texture and noise.
    """
    height, width, _ = pixels.shape
    patch = max(4, min(height, width) // 40)
    corners = np.concatenate([
        pixels[:patch, :patch].reshape(-1, 3),
        pixels[:patch, -patch:].reshape(-1, 3),
        pixels[-patch:, :patch].reshape(-1, 3),
        pixels[-patch:, -patch:].reshape(-1, 3),
    ])
    return np.median(corners, axis=0)


def measure_border(pixels: np.ndarray, box: tuple[int, int, int, int]) -> int:
    """How far the icon's own border extends beyond the coloured field.

    Walks outward from the middle of each edge until it reaches the page
    colour. Anything before that — a white rim, a drop shadow's inner edge —
    belongs to the icon and should be kept.

    The border is drawn at a uniform width, so we take the *median* of the
    four readings rather than the smallest: any one edge can read short where
    the artwork's own shading happens to match the page, and one bad reading
    should not throw the whole crop away.

    A few pixels then come off the result, and the whole thing is clamped to
    the margin actually available. That asymmetry is deliberate: cropping a
    pixel too few merely trims an invisible sliver off a white rim, while
    cropping a pixel too many leaves a fringe of the original page stuck to
    the icon, which is glaring on a dark panel.
    """
    x0, y0, x1, y1 = box
    height, width, _ = pixels.shape
    middle_y, middle_x = (y0 + y1) // 2, (x0 + x1) // 2
    page = page_colour(pixels)

    walks = {
        "left":   [(middle_y, x) for x in range(x0 - 1, -1, -1)],
        "right":  [(middle_y, x) for x in range(x1 + 1, width)],
        "top":    [(y, middle_x) for y in range(y0 - 1, -1, -1)],
        "bottom": [(y, middle_x) for y in range(y1 + 1, height)],
    }

    thicknesses = []
    for name, walk in walks.items():
        count = 0
        for y, x in walk:
            if np.abs(pixels[y, x] - page).max() < 24:
                break
            count += 1
        thicknesses.append(count)
        print(f"    border {name:<6} {count:3d} px")

    typical = int(np.median(thicknesses)) - BORDER_SAFETY
    available = min(x0, width - 1 - x1, y0, height - 1 - y1)
    return max(0, min(typical, available))


def rounded_mask(size: int, radius: int) -> Image.Image:
    """An anti-aliased rounded-square alpha mask."""
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=radius * SUPERSAMPLE, fill=255
    )
    return mask.resize((size, size), Image.LANCZOS)


def build(source_path: Path) -> None:
    source = Image.open(source_path).convert("RGB")
    pixels = np.asarray(source).astype(int)
    print(f"  source {source.width}x{source.height}")

    x0, y0, x1, y1 = find_field(pixels)
    print(f"    field  x {x0}..{x1}  y {y0}..{y1}")

    field_radius = measure_corner_radius(pixels, (x0, y0, x1, y1))
    border = measure_border(pixels, (x0, y0, x1, y1))
    print(f"    field radius {field_radius} px, keeping {border} px of border")

    crop = (
        max(0, x0 - border),
        max(0, y0 - border),
        min(source.width, x1 + 1 + border),
        min(source.height, y1 + 1 + border),
    )
    cropped = source.crop(crop)
    print(f"    cropped to {cropped.width}x{cropped.height}")

    # The radius as a fraction of the cropped art, so it survives the squaring.
    outer_radius = field_radius + border
    radius_fraction = outer_radius / ((cropped.width + cropped.height) / 2)

    # One high-quality square master; every size comes from this.
    master_size = max(SIZES) * 2
    master = cropped.resize((master_size, master_size), Image.LANCZOS).convert("RGBA")
    master.putalpha(rounded_mask(master_size, int(radius_fraction * master_size)))

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    written = []

    for size in SIZES:
        image = master.resize((size, size), Image.LANCZOS)
        # Re-cut the corners at the target size: a downscaled mask goes muddy
        # at 16px, and a soft corner reads as a dirty edge.
        image.putalpha(rounded_mask(size, max(1, round(radius_fraction * size))))

        directory = ICON_DIR / "hicolor" / f"{size}x{size}" / "apps"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{APP_ID}.png"
        image.save(path, "PNG", optimize=True)
        written.append(path)

    print()
    for path in written:
        print(f"    {path.relative_to(PROJECT)}  ({path.stat().st_size / 1024:.1f} KB)")
    total = sum(p.stat().st_size for p in written)
    print(f"\n  {len(written)} files, {total / 1024:.0f} KB in total")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    source_path = Path(sys.argv[1]).expanduser()
    if not source_path.is_file():
        print(f"No such file: {source_path}")
        return 1
    print(f"Building Kairos icons from {source_path}")
    build(source_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
