#!/bin/sh
# RAW-only deployment. Never stops processes belonging to the original VISTA.
set -eu
exec /usr/bin/python3 "$(dirname "$0")/scripts/raw/deploy.py" "$@"
