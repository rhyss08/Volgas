"""Processes the real VOLGAS logo (../Volgas logo.jpg - a grey VG monogram on a
dark charcoal background, ~57,57,57) into a transparent-background app icon.

Background and logo grey are well separated (bg ~57, logo ~124-127), so a
distance-from-background threshold with a soft-edged transition band (for
antialiasing against JPEG compression noise) cleanly keys out the background.

The source image also has a small sparkle mark in the bottom-right corner,
outside the main monogram - excluded when computing the crop bbox so it
doesn't force extra padding around the logo, then cropped off entirely.

Run again after replacing the source JPG. Regenerates volgas_logo.png and volgas.ico.
"""

from PIL import Image

SRC = "../Volgas logo.jpg"
BG = (57, 57, 57)
# Distance below LOW is fully transparent (background), above HIGH is fully
# opaque (logo); linear ramp between the two feathers the edge.
LOW, HIGH = 25, 55
# Fraction of width/height, from the bottom-right, treated as the sparkle
# mark and excluded from both the crop bbox and the final image.
STAR_FRACTION = 0.2
# Extra margin added around the cropped monogram, as a fraction of its size.
CROP_MARGIN = 0.04


def make_transparent(img):
    img = img.convert("RGB")
    px = img.load()
    out = Image.new("RGBA", img.size)
    out_px = out.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            dist = ((r - BG[0]) ** 2 + (g - BG[1]) ** 2 + (b - BG[2]) ** 2) ** 0.5
            if dist <= LOW:
                alpha = 0
            elif dist >= HIGH:
                alpha = 255
            else:
                alpha = round((dist - LOW) / (HIGH - LOW) * 255)
            out_px[x, y] = (r, g, b, alpha)
    return out


def crop_to_monogram(img):
    w, h = img.size
    star_x0, star_y0 = int(w * (1 - STAR_FRACTION)), int(h * (1 - STAR_FRACTION))
    # Blank out the sparkle region so it can't appear in the final crop.
    px = img.load()
    for y in range(star_y0, h):
        for x in range(star_x0, w):
            px[x, y] = (0, 0, 0, 0)

    bbox = img.getbbox()
    minx, miny, maxx, maxy = bbox
    bw, bh = maxx - minx, maxy - miny
    margin = round(max(bw, bh) * CROP_MARGIN)
    minx, miny = max(0, minx - margin), max(0, miny - margin)
    maxx, maxy = min(w, maxx + margin), min(h, maxy + margin)

    cropped = img.crop((minx, miny, maxx, maxy))
    # Pad to square so the icon isn't stretched/distorted by .ico resizing.
    side = max(cropped.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2))
    return square


if __name__ == "__main__":
    img = Image.open(SRC)
    result = crop_to_monogram(make_transparent(img))
    result.save("volgas_logo.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    result.save("volgas.ico", sizes=sizes)
    print("wrote volgas_logo.png and volgas.ico")
