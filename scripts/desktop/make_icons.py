"""Regenerate the desktop app icons from ``apps/desktop/resources/icon.svg``.

Writes ``icon.png`` (1024), ``icon.icns`` and ``icon.ico`` next to the SVG. The
outputs are committed, so this only needs a rerun when the mark changes, and
CI never needs it. macOS only: renders with headless Google Chrome (SVG to
PNG, alpha preserved), resizes with ``sips`` and packs with ``iconutil``. The
ICO is written here: 16-128 as 32-bit DIBs, which every Windows icon consumer
reads (including makensis), and 256 as PNG.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[2] / "apps" / "desktop" / "resources"
SVG = RESOURCES / "icon.svg"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ICNS_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))
ICO_DIB_SIZES = (16, 24, 32, 48, 64, 128)
ICO_PNG_SIZE = 256


def render_png(out: Path, work: Path) -> None:
    page = work / "icon.html"
    page.write_text(
        "<!doctype html><html><body style='margin:0;background:transparent'>"
        + SVG.read_text(encoding="utf-8") + "</body></html>", encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--user-data-dir={work / 'chrome'}", "--default-background-color=00000000",
         "--window-size=1024,1024", f"--screenshot={out}", page.as_uri()],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def resize(source: Path, size: int, out: Path) -> None:
    subprocess.run(["sips", "-z", str(size), str(size), str(source), "--out", str(out)],
                   check=True, stdout=subprocess.DEVNULL)


def png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG into top-down RGBA rows."""
    pos, idat = 8, bytearray()
    width = height = channels = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            channels = {2: 3, 6: 4}.get(color, 0)
            if depth != 8 or not channels or interlace:
                raise SystemExit(f"unsupported PNG: depth={depth} color={color} interlace={interlace}")
        elif kind == b"IDAT":
            idat += chunk
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    previous = bytearray(stride)
    out = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                row[i] = (row[i] + left) & 255
            elif kind == 2:
                row[i] = (row[i] + up) & 255
            elif kind == 3:
                row[i] = (row[i] + (left + up) // 2) & 255
            elif kind == 4:
                estimate = left + up - corner
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                row[i] = (row[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        previous = row
        if channels == 4:
            out += row
        else:
            for i in range(0, stride, 3):
                out += row[i:i + 3] + b"\xff"
    return width, height, bytes(out)


def ico_dib(png: bytes) -> bytes:
    width, height, rgba = png_rgba(png)
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    bgra = bytearray()
    for y in range(height - 1, -1, -1):  # DIB rows are bottom-up
        row = rgba[y * width * 4:(y + 1) * width * 4]
        for i in range(0, len(row), 4):
            bgra += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))
    mask_stride = ((width + 31) // 32) * 4  # all-zero AND mask: alpha decides
    return header + bytes(bgra) + bytes(mask_stride * height)


def write_ico(out: Path, images: list[tuple[int, bytes]]) -> None:
    offset = 6 + 16 * len(images)
    directory, body = bytearray(), bytearray()
    for size, blob in images:
        byte_size = 0 if size >= 256 else size  # 0 means 256 in ICONDIRENTRY
        directory += struct.pack("<BBBBHHII", byte_size, byte_size, 0, 0, 1, 32, len(blob), offset + len(body))
        body += blob
    out.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + bytes(directory) + bytes(body))


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("make_icons.py needs macOS (sips, iconutil); the generated icons are committed")
    with tempfile.TemporaryDirectory(prefix="p2r-icons-") as tmp:
        work = Path(tmp)
        master = RESOURCES / "icon.png"
        render_png(master, work)

        iconset = work / "icon.iconset"
        iconset.mkdir()
        for size, scale in ICNS_SIZES:
            suffix = "@2x" if scale == 2 else ""
            resize(master, size * scale, iconset / f"icon_{size}x{size}{suffix}.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(RESOURCES / "icon.icns")], check=True)

        images = []
        for size in (*ICO_DIB_SIZES, ICO_PNG_SIZE):
            png = work / f"ico-{size}.png"
            resize(master, size, png)
            images.append((size, png.read_bytes() if size == ICO_PNG_SIZE else ico_dib(png.read_bytes())))
        write_ico(RESOURCES / "icon.ico", images)
    for name in ("icon.png", "icon.icns", "icon.ico"):
        print(f"{RESOURCES / name} ({(RESOURCES / name).stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
