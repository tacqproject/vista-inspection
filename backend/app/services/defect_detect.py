"""
Lightweight computer-vision crack/defect detector (no OpenCV — numpy + scipy +
Pillow only). This is a heuristic assist, not a trained model: it highlights
thin dark line structures (typical of cracks) via a morphological top-hat, keeps
elongated connected components, and estimates their length/width in pixels.

Results are suggestions the inspector reviews and accepts — every produced
annotation is flagged ai_detected with a confidence so it is never mistaken for
a verified finding.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw


def analyze_image(path: Path, max_dim: int = 1100) -> dict:
    img = Image.open(path).convert("RGB")
    W0, H0 = img.size
    scale = min(1.0, max_dim / max(W0, H0))
    work = img.resize((max(1, int(W0 * scale)), max(1, int(H0 * scale)))) if scale < 1 else img
    g = np.asarray(work.convert("L"), dtype=np.float32) / 255.0
    H, W = g.shape

    # Top-hat on the inverted image → bright response where thin *dark* lines are.
    inv = 1.0 - g
    opened = ndimage.grey_opening(inv, size=(9, 9))
    resp = np.clip(inv - opened, 0, None)
    if resp.max() > 0:
        resp = resp / resp.max()

    mask = resp > 0.32
    mask = ndimage.binary_opening(mask, structure=np.ones((2, 2)))
    lbl, n = ndimage.label(mask)

    regions = []
    total_length_px = 0.0
    max_width_px = 0.0
    inv_scale = 1.0 / scale if scale else 1.0

    for i in range(1, n + 1):
        ys, xs = np.where(lbl == i)
        area = xs.size
        if area < 45:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        length = float(max(bw, bh))
        elong = length / max(1.0, float(min(bw, bh)))
        if elong < 2.3:                      # keep only crack-like elongated blobs
            continue
        width = area / max(1.0, length)      # mean width ≈ area / length
        length_px = length * inv_scale
        width_px = width * inv_scale
        regions.append({
            "bbox": [x0 / W, y0 / H, bw / W, bh / H],   # normalized (0..1)
            "length_px": round(length_px, 1),
            "width_px": round(width_px, 2),
            "elongation": round(elong, 1),
        })
        total_length_px += length_px
        max_width_px = max(max_width_px, width_px)

    regions.sort(key=lambda r: -r["length_px"])
    crack_ratio = float(mask.mean())

    # Heuristic severity from the widest detected line (relative to image size).
    rel_w = max_width_px / max(W0, H0) if regions else 0.0
    if not regions:
        severity = "low"
    elif rel_w > 0.006 or max_width_px > 9:
        severity = "critical"
    elif rel_w > 0.003 or max_width_px > 5:
        severity = "high"
    elif rel_w > 0.0015:
        severity = "medium"
    else:
        severity = "low"

    # Confidence: more/longer elongated evidence → higher, capped.
    conf = min(0.9, 0.25 + 0.12 * len(regions) + min(0.3, total_length_px / max(W0, H0) * 0.4))

    overlay_b64 = _overlay(img, regions)

    return {
        "found": bool(regions),
        "count": len(regions),
        "regions": regions[:20],
        "max_width_px": round(max_width_px, 2),
        "total_length_px": round(total_length_px, 1),
        "crack_ratio": round(crack_ratio, 4),
        "severity_guess": severity,
        "defect_type_guess": "crack" if regions else "other",
        "confidence": round(conf, 2),
        "image_width": W0,
        "image_height": H0,
        "overlay": overlay_b64,
    }


def _overlay(img: Image.Image, regions: list[dict]) -> str:
    """Draw detected crack boxes on the full-res image → base64 PNG data-URL."""
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    W, H = canvas.size
    lw = max(2, W // 400)
    for idx, r in enumerate(regions, 1):
        x, y, bw, bh = r["bbox"]
        box = [x * W, y * H, (x + bw) * W, (y + bh) * H]
        draw.rectangle(box, outline=(239, 68, 68), width=lw)
        draw.text((box[0], max(0, box[1] - 14)), f"#{idx}", fill=(239, 68, 68))
    buf = BytesIO()
    canvas.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
