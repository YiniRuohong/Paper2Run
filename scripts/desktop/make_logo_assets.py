"""Regenerate every Paper2Run logo asset from the source logo (black glyph on white).

    python3 scripts/desktop/make_logo_assets.py [--source PATH] [--android-res DIR]
    python3 scripts/desktop/make_icons.py        # then: icon.png / icon.icns / icon.ico

Writes (all committed; rerun only when the logo changes):
  apps/desktop/resources/brand/logo-source.png   the original
  apps/desktop/resources/brand/logo-mark.png     glyph only, transparent, tight square (1024)
  apps/desktop/resources/icon.svg                app icon source: white rounded tile + glyph (make_icons.py input)
  apps/desktop/splash.html                       the splash mark (same tile, inlined)
  apps/web/src/ui/logo-mark.png                  sidebar mark, used as a CSS mask so it follows the theme colour
  apps/web/public/favicon.png                    tile, 256
  --android-res: launcher icons (adaptive foreground + legacy/round mipmaps) for apps/android on feat/tablet-remote

Needs Pillow (python3 -m pip install pillow); the build itself never runs this.
"""
from __future__ import annotations

import argparse
import base64
import io
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "apps" / "desktop" / "resources"
DEFAULT_SOURCE = RESOURCES / "brand" / "logo-source.png"
INK = (17, 17, 19)
WHITE_CUTOFF, BLACK_CUTOFF = 235, 60   # luminance: >= white is transparent, <= black is opaque


def glyph(source: Path) -> Image.Image:
    """The glyph as an RGBA image on transparent, cropped to a tight square."""
    lum = ImageOps.grayscale(Image.open(source).convert("RGB"))
    span = WHITE_CUTOFF - BLACK_CUTOFF
    alpha = lum.point(lambda v: 0 if v >= WHITE_CUTOFF else 255 if v <= BLACK_CUTOFF else round(255 * (WHITE_CUTOFF - v) / span))
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if box is None:
        raise SystemExit("no glyph found in the source image")
    alpha = alpha.crop(box)
    side = max(alpha.size)
    square = Image.new("L", (side, side), 0)
    square.paste(alpha, ((side - alpha.width) // 2, (side - alpha.height) // 2))
    mark = Image.new("RGBA", (side, side), INK + (0,))
    mark.putalpha(square)
    return mark


def scaled(mark: Image.Image, size: int) -> Image.Image:
    return mark.resize((size, size), Image.LANCZOS)


def tile(mark: Image.Image, size: int, *, glyph_ratio=0.64, radius_ratio=0.2233, round_=False, margin_ratio=0.0) -> Image.Image:
    """White tile (rounded square or circle) with the glyph centred, on transparent."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    margin = round(size * margin_ratio)
    body = size - 2 * margin
    shape = Image.new("L", (size * 4, size * 4), 0)
    draw = ImageDraw.Draw(shape)
    rect = (margin * 4, margin * 4, (size - margin) * 4 - 1, (size - margin) * 4 - 1)
    if round_:
        draw.ellipse(rect, fill=255)
    else:
        draw.rounded_rectangle(rect, radius=round(body * radius_ratio * 4), fill=255)
    plate = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    plate.putalpha(shape.resize((size, size), Image.LANCZOS))
    out.alpha_composite(plate)
    g = round(body * glyph_ratio)
    out.alpha_composite(scaled(mark, g), ((size - g) // 2, (size - g) // 2))
    return out


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def tile_svg(mark: Image.Image, *, gradient_id="p2r-tile") -> str:
    """824-unit tile at (100,100) in a 1024 viewBox (macOS icon grid) with the glyph embedded as PNG."""
    data = base64.b64encode(png_bytes(scaled(mark, 640))).decode()
    g = 528  # glyph box: 64% of the 824 tile
    at = 100 + (824 - g) // 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1024 1024" width="1024" height="1024">'
            f'<defs><linearGradient id="{gradient_id}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#EEF0F3"/></linearGradient></defs>'
            f'<rect x="100" y="100" width="824" height="824" rx="184" fill="url(#{gradient_id})"/>'
            f'<rect x="101" y="101" width="822" height="822" rx="183" fill="none" stroke="#000" stroke-opacity=".08" stroke-width="2"/>'
            f'<image x="{at}" y="{at}" width="{g}" height="{g}" href="data:image/png;base64,{data}" xlink:href="data:image/png;base64,{data}"/>'
            f'</svg>')


def write_splash(svg: str) -> None:
    splash = ROOT / "apps" / "desktop" / "splash.html"
    text = splash.read_text(encoding="utf-8")
    inner = svg.split(">", 1)[1].rsplit("</svg>", 1)[0]
    mark = f'<svg class="mark" viewBox="100 100 824 824" aria-hidden="true">{inner}</svg>'
    updated, count = re.subn(r'<svg class="mark".*?</svg>', lambda _: mark, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit("splash.html: <svg class=\"mark\"> not found")
    splash.write_text(updated, encoding="utf-8")


def write_android(mark: Image.Image, res: Path) -> None:
    """Adaptive icon (white background + glyph foreground in the 66 dp safe zone) and legacy mipmaps."""
    (res / "drawable" / "ic_launcher_foreground.xml").unlink(missing_ok=True)
    (res / "drawable" / "ic_launcher_background.xml").unlink(missing_ok=True)
    nodpi = res / "drawable-nodpi"
    nodpi.mkdir(exist_ok=True)
    foreground = Image.new("RGBA", (432, 432), (0, 0, 0, 0))   # 108 dp at 4x
    g = 236                                                   # ~55% of 108 dp: inside the 66 dp safe circle
    foreground.alpha_composite(scaled(mark, g), ((432 - g) // 2, (432 - g) // 2))
    foreground.save(nodpi / "ic_launcher_foreground.png", optimize=True)
    values = res / "values" / "ic_launcher_colors.xml"
    values.write_text('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                      '    <color name="ic_launcher_background">#FFFFFF</color>\n</resources>\n', encoding="utf-8")
    adaptive = ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/ic_launcher_background" />\n'
                '    <foreground android:drawable="@drawable/ic_launcher_foreground" />\n'
                '    <monochrome android:drawable="@drawable/ic_launcher_foreground" />\n'
                '</adaptive-icon>\n')
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        (res / "mipmap-anydpi-v26" / name).write_text(adaptive, encoding="utf-8")
    for density, size in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        folder = res / f"mipmap-{density}"
        tile(mark, size, margin_ratio=0.0833, glyph_ratio=0.66).save(folder / "ic_launcher.webp", "WEBP", lossless=True)
        tile(mark, size, margin_ratio=0.0833, glyph_ratio=0.62, round_=True).save(folder / "ic_launcher_round.webp", "WEBP", lossless=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--android-res", type=Path, help="apps/android/app/src/main/res of the tablet app")
    args = parser.parse_args()
    brand = RESOURCES / "brand"
    brand.mkdir(parents=True, exist_ok=True)
    if args.source.resolve() != DEFAULT_SOURCE.resolve():
        shutil.copyfile(args.source, DEFAULT_SOURCE)
    mark = glyph(DEFAULT_SOURCE)
    scaled(mark, 1024).save(brand / "logo-mark.png", optimize=True)
    svg = tile_svg(mark)
    (RESOURCES / "icon.svg").write_text(svg + "\n", encoding="utf-8")
    write_splash(tile_svg(mark, gradient_id="tile"))
    scaled(mark, 256).save(ROOT / "apps" / "web" / "src" / "ui" / "logo-mark.png", optimize=True)
    (ROOT / "apps" / "web" / "public").mkdir(exist_ok=True)
    tile(mark, 256, margin_ratio=0.03).save(ROOT / "apps" / "web" / "public" / "favicon.png", optimize=True)
    if args.android_res:
        write_android(mark, args.android_res)
    print("logo assets written; now run scripts/desktop/make_icons.py for icon.png / .icns / .ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
