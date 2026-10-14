#!/usr/bin/env python3
"""
Mock defect-segmentation inferer — implements the VISTA inference contract WITHOUT
the real model, so the whole pipeline (trigger → progress → results → overlays →
PDF → UI) can be tested before dacl10k is set up.

Contract (same as the real ai/dacl10k_infer.py):
    mock_infer.py --images <dir> --out <dir> --tile 1024 --overlap 128
writes  <out>/results.json  and  <out>/overlay_<name>.jpg,
prints  PROGRESS <pct> <message>  lines.

Produces deterministic-ish pseudo-random coverage + a tinted overlay so the UI
shows something plausible. NOT a real detector.
"""

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

CLASSES = [
    "Crack", "ACrack", "Wetspot", "Efflorescence", "Rust", "Rockpocket",
    "Hollowareas", "Cavity", "Spalling", "Graffiti", "Weathering",
    "Restformwork", "ExposedRebars", "Bearing", "EJoint", "Drainage",
    "PEquipment", "JTape", "WConccor",
]
HIGHLIGHT = ["Crack", "Spalling", "Rust", "Efflorescence", "Weathering"]
IMG_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tile", type=int, default=1024)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    images_dir, out_dir = Path(args.images), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in images_dir.glob("*") if f.suffix.lower() in IMG_EXT)
    total = len(files) or 1

    results = {"classes": CLASSES, "images": []}
    for i, f in enumerate(files):
        print(f"PROGRESS {int(5 + 90 * i / total)} Analyzuji {f.name}", flush=True)
        rng = random.Random(hash(f.name) & 0xffffffff)
        coverage = {c: 0.0 for c in CLASSES}
        try:
            img = Image.open(f).convert("RGB")
        except Exception:
            continue
        W, H = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        for c in rng.sample(HIGHLIGHT, k=rng.randint(0, 2)):
            cov = round(rng.uniform(0.005, 0.08), 4)
            coverage[c] = cov
            # draw a translucent blob to fake the detected region
            bw, bh = int(W * cov * 4), int(H * cov * 4)
            x, y = rng.randint(0, max(1, W - bw)), rng.randint(0, max(1, H - bh))
            draw.rectangle([x, y, x + bw, y + bh], fill=(239, 68, 68, 90),
                           outline=(239, 68, 68, 220), width=max(2, W // 400))

        overlay_name = f"overlay_{f.stem}.jpg"
        img.save(out_dir / overlay_name, "JPEG", quality=85)
        results["images"].append(
            {"filename": f.name, "overlay": overlay_name, "coverage": coverage}
        )

    (out_dir / "results.json").write_text(json.dumps(results))
    print("PROGRESS 100 Hotovo", flush=True)


if __name__ == "__main__":
    main()
