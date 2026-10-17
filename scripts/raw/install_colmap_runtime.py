#!/usr/bin/env python3
"""Pinned, private CPU SfM tools; never change shared or system packages."""
import json
from pathlib import Path
import subprocess

ROOT = Path('/media/epr/ssd/vista-raw')
REQUIREMENTS = (
    'pycolmap==4.2.1 --hash=sha256:7627f0bab5746d04d94f91fec0dcb4b8ae0d5592853ab7b549cbd7dd021dd90f\n'
    'numpy==2.3.4 --hash=sha256:a7b2f9a18b5ff9824a6af80de4f37f4ec3c2aab05ef08f51c77a093f5b89adda\n'
)


def main():
    target = ROOT / 'tools/python-colmap-4.2.1'
    base = ROOT / 'tools/python-sfm/python/bin/python3.11'
    if ROOT.resolve() != ROOT or not target.resolve().is_relative_to(ROOT):
        raise SystemExit('Refusing a runtime outside isolated RAW storage')
    if not base.is_file():
        raise SystemExit('First run scripts/raw/install_sfm_runtime.py')
    marker = target / 'installation.json'
    if target.exists():
        if not marker.is_file() or json.loads(marker.read_text()).get('requirements') != REQUIREMENTS:
            raise SystemExit('Existing unverified runtime: inspect it before replacing')
    else:
        subprocess.run([str(base), '-m', 'venv', str(target)], check=True)
        requirements = target / 'requirements-pinned.txt'
        requirements.write_text(REQUIREMENTS)
        subprocess.run([str(target/'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--no-cache-dir', '--only-binary=:all:', '--require-hashes',
                        '-r', str(requirements)], check=True)
        marker.write_text(json.dumps({'python': str(base), 'requirements': REQUIREMENTS}, indent=2))
    subprocess.run([str(target/'bin/python'), '-c',
                    "import pycolmap,numpy; assert pycolmap.__version__=='4.2.1'; "
                    "assert numpy.__version__=='2.3.4'; print('Isolated SfM review runtime is ready')"], check=True)


if __name__ == '__main__':
    main()
