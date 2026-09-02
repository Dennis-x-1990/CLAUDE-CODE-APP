"""Generate the app icon (app.ico) — orange rounded tile with a bolt."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "app.ico"

TOP = (245, 158, 11)     # amber-500
BOTTOM = (180, 83, 9)    # amber-700
WHITE = (255, 255, 255, 255)


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
    radius = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def draw_bolt(img: Image.Image) -> None:
    size = img.size[0]
    s = size / 256.0
    # bolt polygon tuned on a 256 grid
    pts = [(150, 30), (92, 142), (128, 142), (106, 226),
           (180, 106), (140, 106), (172, 30)]
    scaled = [(x * s, y * s) for x, y in pts]
    d = ImageDraw.Draw(img)
    # soft shadow
    shadow = [(x + 3 * s, y + 4 * s) for x, y in scaled]
    d.polygon(shadow, fill=(120, 53, 15, 90))
    d.polygon(scaled, fill=WHITE)


def main() -> None:
    base = gradient_tile(256)
    draw_bolt(base)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written:", OUT)


if __name__ == "__main__":
    main()
