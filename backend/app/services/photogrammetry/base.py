"""
Core types and registries for the photogrammetry pipeline.

Architecture
────────────
A reconstruction run has two phases:

  1. ENGINE — produces raw 3D data (mesh, point cloud…) from photos.
     One engine runs per reconstruction. Engines register via @engine.

  2. POST-PROCESSING — an ordered chain of steps that refine the raw output
     (texture conversion, LOD generation, camera-pose harvesting…).
     Every step runs after every engine; a step that has nothing to do
     (e.g. no EXR textures present) must simply return without side effects.
     Steps register via @postprocess_step(order=N) — lower order runs first.

Both phases share a ReconstructionContext. To add a new processing module,
create a file in postprocess/, decorate an async function, and import the
module in postprocess/__init__.py — nothing else needs to change.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

ProgressCallback = Callable[[int, str], Awaitable[None]]


@dataclass
class ReconstructionContext:
    inspection_id: str
    photos_dir: Path                     # source photos (absolute)
    models_dir: Path                     # output root for this inspection (absolute)
    params: dict = field(default_factory=dict)   # user options (quality, cleanup…)
    progress: ProgressCallback | None = None

    # Filled by the engine:
    obj_file: Path | None = None         # main textured mesh (absolute)
    result: dict = field(default_factory=dict)   # model_path/point_cloud_path/… (absolute Paths)

    async def report(self, pct: int, message: str):
        if self.progress:
            await self.progress(pct, message)


# ─── Engine registry ──────────────────────────────────────────────────────────

@dataclass
class EngineSpec:
    name: str
    run: Callable[[ReconstructionContext], Awaitable[None]]
    check: Callable[[], Awaitable[dict]]   # availability info for /config/engines


ENGINES: dict[str, EngineSpec] = {}


def engine(name: str, check: Callable[[], Awaitable[dict]]):
    """Register a reconstruction engine."""
    def wrap(fn):
        ENGINES[name] = EngineSpec(name=name, run=fn, check=check)
        return fn
    return wrap


# ─── Post-processing registry ─────────────────────────────────────────────────

@dataclass
class StepSpec:
    name: str
    order: int
    run: Callable[[ReconstructionContext], Awaitable[None]]


POSTPROCESS_STEPS: list[StepSpec] = []


def postprocess_step(name: str, order: int):
    """Register a post-processing step; lower `order` runs earlier."""
    def wrap(fn):
        POSTPROCESS_STEPS.append(StepSpec(name=name, order=order, run=fn))
        POSTPROCESS_STEPS.sort(key=lambda s: s.order)
        return fn
    return wrap
