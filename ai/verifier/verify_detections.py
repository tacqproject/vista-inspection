"""Second stage of the AI defect detection: a classifier trained on manually
reviewed detections (real defect vs. false alarm — terrain, vegetation,
shadows, structural edges, dried mud, objects) scores every detection.
Writes `verify` (0–1) into each detection of detections.json in place.

usage: python verify_detections.py <detections.json> <photos_dir> [model.pt]
Runs in the dacl10k env (torch + timm)."""
import json, os, sys
from pathlib import Path
import cv2, numpy as np, torch, torch.nn as nn, timm

S = 288                                   # crop size the model was trained on (then resized to IMG)
CL = {"crack": 0, "spalling": 1, "corrosion": 2}


class Net(nn.Module):
    def __init__(s):
        super().__init__()
        s.b = timm.create_model("convnext_tiny", pretrained=False, in_chans=4, num_classes=0)
        s.emb = nn.Embedding(3, 16)
        s.head = nn.Sequential(nn.Dropout(0.3), nn.Linear(s.b.num_features + 16, 1))

    def forward(s, x, c):
        return s.head(torch.cat([s.b(x), s.emb(c)], 1)).squeeze(1)


def crop(photo, poly):
    """Same framing as the review sheets: detection bbox × 1.5 + 60 px, ≥ 160 px."""
    h, w = photo.shape[:2]
    x0, y0 = poly.min(0); x1, y1 = poly.max(0)
    side = max(max(x1 - x0, y1 - y0) * 1.5 + 60, 160)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    X0, Y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    X1, Y1 = int(min(w, X0 + side)), int(min(h, Y0 + side))
    c = photo[Y0:Y1, X0:X1]
    m = np.zeros(c.shape[:2], np.uint8)
    cv2.fillPoly(m, [(poly - [X0, Y0]).astype(np.int32)], 255)
    out = np.zeros((S, S, 4), np.uint8)
    out[:, :, :3] = cv2.resize(c, (S, S), interpolation=cv2.INTER_AREA)[:, :, ::-1]
    out[:, :, 3] = cv2.resize(m, (S, S), interpolation=cv2.INTER_NEAREST)
    return out


def main():
    det_file, photos = Path(sys.argv[1]), Path(sys.argv[2])
    model = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).with_name("verifier.pt")
    ck = torch.load(model, map_location="cpu")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    net = Net(); net.load_state_dict(ck["state"]); net.to(dev).eval()
    img = ck["img"]
    mean = torch.tensor([0.485, 0.456, 0.406, 0.5], device=dev).view(1, 4, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225, 0.5], device=dev).view(1, 4, 1, 1)
    data = json.loads(det_file.read_text())
    ims = [im for im in data["images"] if im["detections"]]
    for n, im in enumerate(ims):
        ph = cv2.imread(str(photos / im["filename"]))
        if ph is None:
            continue
        sx, sy = ph.shape[1] / im["width"], ph.shape[0] / im["height"]
        x = np.stack([crop(ph, np.array(d["polygon"], float) * [sx, sy]) for d in im["detections"]])
        c = torch.tensor([CL.get(d["cls"], 1) for d in im["detections"]], device=dev)
        with torch.no_grad():
            t = torch.from_numpy(x).to(dev).permute(0, 3, 1, 2).float() / 255
            t = (torch.nn.functional.interpolate(t, size=img, mode="bilinear", align_corners=False) - mean) / std
            s = sum(torch.sigmoid(net(v, c)) for v in (t, t.flip(3), t.flip(2), torch.rot90(t, 1, (2, 3)))) / 4
        for d, v in zip(im["detections"], s.cpu().numpy()):
            d["verify"] = round(float(v), 4)
        if n % 25 == 0:
            print(f"PROGRESS {int(100 * n / max(len(ims), 1))}", flush=True)
    data["verifier"] = {"model": model.name, "threshold": ck.get("threshold", 0.5)}
    tmp = det_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, det_file.resolve())
    print("verified", sum(len(im["detections"]) for im in ims), flush=True)


if __name__ == "__main__":
    main()
