"""
Extract still frames from an uploaded video for photogrammetry.

Uses the ffmpeg binary bundled with imageio-ffmpeg (no system install). Frames
are sampled at a target FPS, then — if there are more than the cap — reduced by
picking the *sharpest* frame in each of `max_frames` equal time bins. This keeps
full temporal coverage of the object while dropping motion-blurred frames, which
matters a lot for reconstruction quality.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv"}


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _sharpness(path: Path) -> float:
    """Variance of the Laplacian on a downscaled grayscale frame (higher = sharper)."""
    try:
        im = Image.open(path).convert("L")
        im.thumbnail((512, 512))
        a = np.asarray(im, dtype=np.float32)
        return float(ndimage.laplace(a).var())
    except Exception:
        return 0.0


def extract_frames(
    video_path: Path,
    out_dir: Path,
    fps: float = 2.0,
    max_frames: int = 150,
    prefix: str = "vid",
    on_log=None,
) -> list[Path]:
    """Sample `video_path` → JPEG frames in `out_dir`. Returns the kept frames."""
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = _ffmpeg_exe()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        cmd = [
            exe, "-nostdin", "-y", "-i", str(video_path),
            # near-lossless JPEG (q:v 1) so texturing gets the sharpest possible
            # frames — video is already compressed, no need to add more loss.
            "-vf", f"fps={fps}", "-q:v", "1", "-qmin", "1",
            str(tmp_dir / "f_%05d.jpg"),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg selhal: {proc.stderr[-500:]}")

        frames = sorted(tmp_dir.glob("f_*.jpg"))
        if not frames:
            raise RuntimeError("Z videa se nepodařilo získat žádné snímky")

        if len(frames) > max_frames:
            # bin by time, keep the sharpest frame per bin (coverage + sharpness)
            if on_log:
                on_log(f"Vybírám {max_frames} nejostřejších z {len(frames)} snímků…")
            scored = [(f, _sharpness(f)) for f in frames]
            chosen = []
            n = len(scored)
            for b in range(max_frames):
                lo = b * n // max_frames
                hi = max(lo + 1, (b + 1) * n // max_frames)
                best = max(scored[lo:hi], key=lambda t: t[1])
                chosen.append(best[0])
        else:
            chosen = frames

        kept: list[Path] = []
        for i, f in enumerate(chosen, 1):
            dest = out_dir / f"{prefix}_{i:05d}.jpg"
            dest.write_bytes(f.read_bytes())
            kept.append(dest)

    return kept
