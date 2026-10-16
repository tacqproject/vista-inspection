#!/bin/sh
# Compatibility entry point for this RAW branch; uses the restricted job broker.
set -eu
exec "$(dirname "$0")/raw/meshroom" "$@"
