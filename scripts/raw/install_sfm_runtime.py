#!/usr/bin/env python3
"""Install only RAW's private Python ABI for Meshroom 2025.1 bindings."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

ROOT = Path('/media/epr/ssd/vista-raw')
RELEASE = '20261013'
ASSET = 'cpython-3.11.17+20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
SHA256 = '3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522'
URL = f'https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{ASSET}'


def main():
    target = ROOT / 'tools/python-sfm'
    if ROOT.resolve() != ROOT or not target.resolve().is_relative_to(ROOT):
        raise SystemExit('Refusing a runtime path outside isolated RAW storage')
    target.mkdir(parents=True, exist_ok=True)
    marker = target / 'installation.json'
    python = target / 'python/bin/python3.11'
    if python.is_file() and marker.is_file() and json.loads(marker.read_text()).get('sha256') == SHA256:
        print('Isolated SfM Python is already installed')
        return
    if (target / 'python').exists():
        raise SystemExit('Existing unverified runtime: inspect it before replacing')
    with tempfile.TemporaryDirectory(prefix='.install-', dir=target) as temp:
        archive = Path(temp) / ASSET
        digest = hashlib.sha256()
        with urllib.request.urlopen(URL, timeout=60) as source, archive.open('wb') as out:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != SHA256:
            raise SystemExit('Python archive checksum mismatch')
        with tarfile.open(archive, 'r:gz') as bundle:
            bundle.extractall(temp, filter='data')
        (Path(temp) / 'python').rename(target / 'python')
    marker.write_text(json.dumps({'release': RELEASE, 'asset': ASSET, 'url': URL, 'sha256': SHA256}, indent=2))
    print('Installed', python)


if __name__ == '__main__':
    main()
