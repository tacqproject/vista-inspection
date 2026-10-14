"""
Post-processing step modules — importing them registers each step.

To add a new 3D-processing module:
  1. create a file here with an async function taking ReconstructionContext,
  2. decorate it with @postprocess_step("name", order=N),
  3. import the module below.
"""

from . import openmvs, refine, cameras, textures, lods, tiles, mobile, quality, defects  # noqa: F401
from .lods import generate_lods  # noqa: F401  (re-export for standalone use)
