"""Draw the brand icon (a sun with a euro sign above a price line) as PNG, standard library only.

    python tools/make_icon.py      # writes custom_components/referenzmarktwert/brand/icon.png (+ @2x)

Anti-aliasing by supersampling: every pixel is the average of SS x SS samples.
"""

import itertools
import math
import struct
import zlib
from pathlib import Path

BRAND = Path(__file__).resolve().parents[1] / "custom_components/referenzmarktwert/brand"
SUN = (247, 181, 0)  # amber
LINE = (3, 169, 244)  # Home Assistant blue
SS = 4


def euro(dx: float, dy: float) -> bool:
    """A euro sign centred in the sun: a "C" open to the right, crossed by two bars."""
    ex, ey = dx + 0.015, dy  # the C sits slightly right, so that the bars stick out on the left
    r = math.hypot(ex, ey)
    opening = abs(math.atan2(ey, ex)) < math.radians(48)
    if 0.083 <= r <= 0.125 and not opening:
        return True
    return any(abs(dy - bar) <= 0.0125 and -0.155 <= dx <= 0.035 for bar in (-0.024, 0.024))


def inside(x: float, y: float) -> tuple[int, int, int] | None:
    """Colour of the shape at (x, y) in a 0..1 square, or None for transparent."""
    cx, cy = 0.5, 0.42
    dx, dy = x - cx, y - cy
    r = math.hypot(dx, dy)
    if r <= 0.2:
        return LINE if euro(dx, dy) else SUN
    # 12 rays: short rounded bars between radius 0.27 and 0.38
    angle = math.atan2(dy, dx) % (2 * math.pi / 12)
    near = min(angle, 2 * math.pi / 12 - angle) * r
    if 0.27 <= r <= 0.38 and near <= 0.025:
        return SUN
    # a falling-then-rising price line along the bottom
    points = [(0.08, 0.80), (0.30, 0.88), (0.50, 0.78), (0.70, 0.84), (0.92, 0.70)]
    for (x1, y1), (x2, y2) in itertools.pairwise(points):
        vx, vy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * vx + (y - y1) * vy) / (vx * vx + vy * vy)))
        if math.hypot(x - (x1 + t * vx), y - (y1 + t * vy)) <= 0.035:
            return LINE
    return None


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type "none"
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    colour = inside((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if colour:
                        r, g, b, a = r + colour[0], g + colour[1], b + colour[2], a + 1
            row += bytes((r // a, g // a, b // a, 255 * a // (SS * SS))) if a else bytes(4)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    BRAND.mkdir(exist_ok=True)
    (BRAND / "icon.png").write_bytes(render(256))
    (BRAND / "icon@2x.png").write_bytes(render(512))
    print("written to", BRAND)
