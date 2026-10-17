"""Fixed CPU stages launched by the isolated broker with a separate RAM limit."""
import asyncio
import json
from pathlib import Path
import sys
import traceback

from .runtime import atomic_json


def run(stage, p):
    if stage.startswith('region-'):
        from . import regional as R
        if stage == 'region-plan':
            return R.make_plan(Path(p['sfm']), Path(p['work']))
        if stage == 'region-prepare':
            return R.prepare_region(Path(p['sfm']), Path(p['global_work']), Path(p['plan']),
                                    Path(p['work']), p['region'], p['max_views'])
        if stage == 'region-crop':
            return R.crop_region(Path(p['mesh']), Path(p['output']), Path(p['plan']), p['region'], p['wide'])
        if stage == 'region-bake':
            return R.bake_region(Path(p['work']), Path(p['mesh']), Path(p['output']), Path(p['plan']), p['region'])
        if stage == 'region-merge':
            return R.merge_regions(Path(p['plan']), Path(p['output']))
        if stage == 'region-tiles':
            return R.region_tiles(Path(p['plan']), Path(p['output']))
    if stage == 'export-colmap':
        from .openmvs_bridge import export_colmap
        return export_colmap(Path(p['sfm']), Path(p['work']))
    if stage == 'clean-mesh':
        from .postprocess.refine import _clean_mesh
        return _clean_mesh(p['mesh'], p['output'], None, False, p['keep_largest'])
    if stage == 'texture-ply':
        from .openmvs_bridge import _textured_ply_to_obj
        _textured_ply_to_obj(Path(p['mesh']), Path(p['output']))
        return {'output': p['output']}
    if stage == 'normal-prior':
        from app.services.normal_prior import run as denoise
        return denoise(p['mesh'], p['output'], p['work'], p['normals'])
    if stage == 'lods':
        from .postprocess.lods import generate_lods
        out = asyncio.run(generate_lods(Path(p['mesh'])))
        if not out:
            raise RuntimeError('Nepodařilo se vytvořit model pro webový prohlížeč.')
        return {'model': str(out)}
    if stage == 'tiles':
        from app.services.tiles import build_tiles
        build_tiles(Path(p['mesh']), workers=1)
        return {'completed': True}
    if stage == 'quality-report':
        from app.services.quality_report import build_report
        return build_report(Path(p['model']), Path(p['photos']))
    raise ValueError('Unknown Python stage')


if __name__ == '__main__':
    stage, payload = sys.argv[1:]
    p = json.loads(payload)
    try:
        result = run(stage, p)
        import resource
        print(f"Peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB", flush=True)
        atomic_json(Path(p['result']), result)
    except Exception as exc:
        traceback.print_exc()
        atomic_json(Path(p['result']), {'error': str(exc)})
        sys.exit(1)
