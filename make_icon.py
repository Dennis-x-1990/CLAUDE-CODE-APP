"""Generate the app icon (app.ico) - coral tile with a Claude-style starburst."""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "app.ico"

TOP = (224, 138, 100)    # light coral
BOTTOM = (190, 88, 48)   # deep terracotta
CREAM = (255, 246, 236)


def gradient_tile(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        r = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
        g = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
        b = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def draw_starburst(img: Image.Image) -> None:
    size = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = size / 2
    n = 12
    r_in = size * 0.145
    w = max(int(size * 0.085), 3)
    rad = w / 2
    for i in range(n):
        a = math.radians(-90 + i * 360.0 / n)
        r_out = size * (0.305 if i % 2 == 0 else 0.268)
        x1, y1 = cx + r_in * math.cos(a), cy + r_in * math.sin(a)
        x2, y2 = cx + r_out * math.cos(a), cy + r_out * math.sin(a)
        d.line([x1, y1, x2, y2], fill=CREAM, width=w)
        for (x, y) in ((x1, y1), (x2, y2)):
            d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=CREAM)


def main() -> None:
    base = gradient_tile(256)
    draw_starburst(base)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                     (128, 128), (256, 256)])
    print("icon written:", OUT)


if __name__ == "__main__":
    main()
