"""Image helpers: downscaling, coordinate grids, pixel sampling and diffs."""

from __future__ import annotations

import io
from collections import Counter

from PIL import Image, ImageChops, ImageDraw, ImageFont


def hex_of(rgb: tuple[int, ...]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb[:3])


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_grid(img: Image.Image, step: int, origin: tuple[int, int] = (0, 0), scale: float = 1.0) -> Image.Image:
    """Overlay gridlines labelled in *device* coordinates.

    `img` may be a crop and/or scaled; `origin` is the device coord of img's (0,0) and
    `scale` is image_px / device_px.
    """
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    font = _font(max(10, int(11 * max(1.0, img.width / 1000))))
    ox, oy = origin
    W, H = img.size
    dev_w, dev_h = W / scale, H / scale

    def lines(start: int, extent: float):
        first = ((start + step - 1) // step) * step
        v = first
        while v < start + extent:
            yield v
            v += step

    for dx in lines(ox, dev_w):
        x = (dx - ox) * scale
        major = dx % (step * 5) == 0
        d.line([(x, 0), (x, H)], fill=(255, 0, 255, 150 if major else 80), width=1)
        d.text((x + 2, 2), str(dx), fill=(255, 0, 255, 255), font=font, stroke_width=2, stroke_fill=(255, 255, 255, 255))
    for dy in lines(oy, dev_h):
        y = (dy - oy) * scale
        major = dy % (step * 5) == 0
        d.line([(0, y), (W, y)], fill=(255, 0, 255, 150 if major else 80), width=1)
        d.text((2, y + 2), str(dy), fill=(255, 0, 255, 255), font=font, stroke_width=2, stroke_fill=(255, 255, 255, 255))
    return Image.alpha_composite(img, overlay).convert("RGB")


def mark_points(img: Image.Image, points: list[tuple[int, int]], origin=(0, 0), scale=1.0) -> Image.Image:
    img = img.convert("RGB").copy()
    d = ImageDraw.Draw(img)
    for (px, py) in points:
        x, y = (px - origin[0]) * scale, (py - origin[1]) * scale
        r = max(6, int(10 * scale))
        d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=3)
        d.line([(x - r - 4, y), (x + r + 4, y)], fill=(255, 0, 0), width=1)
        d.line([(x, y - r - 4), (x, y + r + 4)], fill=(255, 0, 0), width=1)
    return img


def fit(img: Image.Image, max_dim: int) -> tuple[Image.Image, float]:
    """Downscale so the longest side <= max_dim. Returns (image, scale) with scale = out/in."""
    w, h = img.size
    if max_dim <= 0 or max(w, h) <= max_dim:
        return img, 1.0
    s = max_dim / max(w, h)
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS), s


def region_stats(img: Image.Image, box: tuple[int, int, int, int], top: int = 5) -> dict:
    crop = img.crop(box).convert("RGB")
    px = list(crop.getdata())
    n = len(px) or 1
    avg = tuple(round(sum(p[i] for p in px) / n) for i in range(3))
    counts = Counter(px)
    return {
        "box": list(box),
        "pixels": len(px),
        "average": hex_of(avg),
        "unique_colors": len(counts),
        "dominant": [{"color": hex_of(c), "fraction": round(k / n, 4)} for c, k in counts.most_common(top)],
        "uniform": len(counts) == 1,
    }


def diff(a: Image.Image, b: Image.Image, threshold: int = 16) -> dict:
    """Compare two screenshots. threshold = per-channel tolerance (0-255)."""
    if a.size != b.size:
        return {"same_size": False, "a_size": list(a.size), "b_size": list(b.size), "changed_fraction": 1.0}
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    mask = d.point(lambda v: 255 if v > threshold else 0)
    bbox = mask.getbbox()
    hist = mask.histogram()
    changed = hist[255]
    total = a.width * a.height
    return {
        "same_size": True,
        "changed_pixels": changed,
        "changed_fraction": round(changed / total, 5),
        "changed_bbox": list(bbox) if bbox else None,
        "identical": changed == 0,
        "_mask": mask,
    }


def diff_visual(a: Image.Image, b: Image.Image, mask: Image.Image) -> Image.Image:
    """b with changed pixels tinted red, for eyeballing what changed."""
    base = b.convert("RGB").copy()
    red = Image.new("RGB", base.size, (255, 0, 0))
    tinted = Image.blend(base, red, 0.55)
    base.paste(tinted, mask=mask)
    bbox = mask.getbbox()
    if bbox:
        ImageDraw.Draw(base).rectangle(bbox, outline=(255, 0, 0), width=3)
    return base
