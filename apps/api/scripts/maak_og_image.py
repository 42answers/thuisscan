"""
Genereer Open Graph share-image voor Buurtscan (1200x630 px).

Layout:
  - Donkergroene gradient achtergrond (matcht rapport-accentkleur)
  - Buurtscan wordmark groot (witte serif)
  - Tagline + sample chips
  - Subtiele "rapport-preview" decoratie rechts (5-6 stat-tegels)

Run:
    cd apps/api && python3 scripts/maak_og_image.py
    → schrijft naar apps/web/og-image.png

Vereist: Pillow (al in requirements voor static_maps.py).
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# Output naar apps/web/og-image.png
OUT = Path(__file__).resolve().parent.parent.parent / "web" / "og-image.png"

W, H = 1200, 630

# Kleuren — matcht rapport-template
ACCENT = (31, 69, 54)       # #1f4536 donkergroen
ACCENT_DARK = (15, 38, 30)
ACCENT_LIGHT = (54, 110, 90)
WHITE = (255, 255, 255)
WHITE_DIM = (224, 232, 228)
WHITE_MUTE = (180, 200, 192)


def gradient_bg(w, h, top, bottom):
    """Verticale gradient — top → bottom."""
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def find_font(size, *names):
    """Probeer een fonts-bestand uit de gangbare locaties op macOS/Linux/Win."""
    candidates = []
    for n in names:
        for prefix in ["/System/Library/Fonts/", "/Library/Fonts/",
                       "/usr/share/fonts/truetype/dejavu/",
                       "/usr/share/fonts/", "C:/Windows/Fonts/"]:
            candidates.append(prefix + n)
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def text_w(d, txt, font):
    bbox = d.textbbox((0, 0), txt, font=font)
    return bbox[2] - bbox[0]


def make():
    img = gradient_bg(W, H, ACCENT_DARK, ACCENT)
    d = ImageDraw.Draw(img)

    # === Logo: groene "B" in wit vierkant linksboven ===
    LOGO_X, LOGO_Y, LOGO_S = 70, 70, 56
    d.rectangle([LOGO_X, LOGO_Y, LOGO_X + LOGO_S, LOGO_Y + LOGO_S], fill=WHITE)
    f_logo = find_font(40, "Times.ttc", "Times New Roman.ttf", "DejaVuSerif.ttf",
                       "Georgia.ttf", "serif.ttf")
    bb = d.textbbox((0, 0), "B", font=f_logo)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    d.text(
        (LOGO_X + (LOGO_S - bw) / 2 - bb[0],
         LOGO_Y + (LOGO_S - bh) / 2 - bb[1]),
        "B", fill=ACCENT_DARK, font=f_logo,
    )

    # "Buurtscan" naast logo
    f_brand = find_font(34, "HelveticaNeue.ttc", "Helvetica.ttc",
                        "Arial.ttf", "DejaVuSans-Bold.ttf")
    d.text((LOGO_X + LOGO_S + 18, LOGO_Y + 8), "Buurtscan",
           fill=WHITE, font=f_brand)

    # === Hero-tekst: groot serif italic accent ===
    f_hero1 = find_font(76, "Times.ttc", "Georgia.ttf",
                        "DejaVuSerif.ttf")
    f_hero2 = find_font(76, "Times Italic.ttf", "Georgia Italic.ttf",
                        "DejaVuSerif-Italic.ttf", "Times.ttc")

    line1 = "Eén adres."
    line2 = "Volledig rapport"
    line3_a = "uit "
    line3_b = "open data"
    line3_c = "."

    Y0 = 180
    d.text((70, Y0), line1, fill=WHITE, font=f_hero1)
    d.text((70, Y0 + 92), line2, fill=WHITE, font=f_hero1)
    # 3e regel met cursief accent op "open data"
    x = 70
    d.text((x, Y0 + 184), line3_a, fill=WHITE, font=f_hero1)
    x += text_w(d, line3_a, f_hero1)
    d.text((x, Y0 + 184), line3_b, fill=(160, 220, 200), font=f_hero2)
    x += text_w(d, line3_b, f_hero2)
    d.text((x, Y0 + 184), line3_c, fill=WHITE, font=f_hero1)

    # === Sub-tagline ===
    f_sub = find_font(24, "HelveticaNeue.ttc", "Arial.ttf",
                      "DejaVuSans.ttf")
    d.text((70, Y0 + 290),
           "WOZ · Leefbaarheid · Veiligheid · Klimaat · Onderwijs · Verbouwen",
           fill=WHITE_DIM, font=f_sub)

    # === Bottom strip: bron-logos ALS TEKST ===
    f_strip = find_font(16, "CourierNew.ttf", "Courier.ttc",
                        "DejaVuSansMono.ttf")
    strip = "KADASTER · CBS · RVO · POLITIE · RIVM · KNMI · BZK · DUO · LRK · OSM · DSO · MEER"
    sw = text_w(d, strip, f_strip)
    d.text(((W - sw) / 2, H - 60), strip, fill=WHITE_MUTE, font=f_strip)

    # === Decoratie rechts: stat-tegels ===
    BOX_X = 720
    BOX_Y = 200
    BOX_W = 420
    BOX_H = 280

    # Achtergrond-card semitransparant
    overlay = Image.new("RGBA", (BOX_W, BOX_H), (255, 255, 255, 18))
    img.paste(overlay, (BOX_X, BOX_Y), overlay)

    f_label = find_font(13, "CourierNew.ttf", "Courier.ttc",
                        "DejaVuSansMono.ttf")
    f_value = find_font(34, "Times.ttc", "Georgia.ttf",
                        "DejaVuSerif.ttf")

    samples = [
        ("WOZ-WAARDE",   "€ 956.000"),
        ("LEEFBAROMETER", "9 / 9"),
        ("CRIMINALITEIT", "−18% NL"),
        ("BOUWJAAR",     "1930"),
    ]
    for i, (lab, val) in enumerate(samples):
        col = i % 2
        row = i // 2
        cx = BOX_X + 24 + col * (BOX_W // 2)
        cy = BOX_Y + 24 + row * (BOX_H // 2)
        d.text((cx, cy), lab, fill=WHITE_MUTE, font=f_label)
        d.text((cx, cy + 22), val, fill=WHITE, font=f_value)

    # === Bottom-line url ===
    f_url = find_font(20, "HelveticaNeue.ttc", "Arial.ttf",
                      "DejaVuSans.ttf")
    url = "buurtscan.com"
    uw = text_w(d, url, f_url)
    d.text((W - 70 - uw, 86), url, fill=WHITE_DIM, font=f_url)

    img.save(OUT, "PNG", optimize=True)
    print(f"Geschreven: {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    make()
