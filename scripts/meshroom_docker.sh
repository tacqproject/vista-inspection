#!/bin/bash
# Wrapper: spousti Meshroom 2025.1.0 v Docker kontejneru (Ubuntu 22.04 + CUDA 12.2)
# Vyzaduje: docker group membership (viz instrukce nize)

MESHROOM_DIR=/media/epr/ssd/Meshroom-2025.1.0
IMAGE=vista-meshroom:latest
DOCKERFILE_DIR=/home/epr/VISTA/meshroom-docker

# Postav image pokud jeste neexistuje
if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "[meshroom_docker] Stavim Docker image (jednorazove, ~2 min)..."
    docker build -t "$IMAGE" "$DOCKERFILE_DIR"
fi

# Preved vsechny adresarove argumenty na absolutni cesty a namountuj je.
# SEEN sleduje uz pridane cilove adresare (mount pointy) — Docker odmita dva
# -v se stejnym cilem. K duplicite dojde napr. kdyz --cache <dir> a --save
# <dir/project.mg> miri do stejneho adresare (pri opakovanem behu, kdy uz
# project.mg existuje). Prvni mount vyhrava (--cache je rw, pridava se driv).
ARGS=()
MOUNTS=(-v "$MESHROOM_DIR:/meshroom:ro")
declare -A SEEN=(["/meshroom"]=1)
for arg in "$@"; do
    if [[ -d "$arg" ]]; then
        abs=$(realpath "$arg")
        [[ -z "${SEEN[$abs]}" ]] && { MOUNTS+=(-v "$abs:$abs"); SEEN[$abs]=1; }
        ARGS+=("$abs")
    elif [[ -f "$arg" ]]; then
        # soubor (napr. --pipeline sablona.mg) -> namountuj nadrazeny adresar
        abs=$(realpath "$arg")
        dir=$(dirname "$abs")
        [[ -z "${SEEN[$dir]}" ]] && { MOUNTS+=(-v "$dir:$dir:ro"); SEEN[$dir]=1; }
        ARGS+=("$abs")
    else
        ARGS+=("$arg")
    fi
done

# meshroom_batch pise cache do /tmp/MeshroomCache uvnitr kontejneru (ignoruje
# --cache) — namountuj tam hostitelsky adresar, aby cache (a cameras.sfm) prezila
echo "[$(date '+%F %T')] VISTA_MESHROOM_CACHE='$VISTA_MESHROOM_CACHE'" >> /tmp/meshroom_wrapper.log
if [[ -n "$VISTA_MESHROOM_CACHE" && -d "$VISTA_MESHROOM_CACHE" ]]; then
    MOUNTS+=(-v "$(realpath "$VISTA_MESHROOM_CACHE"):/tmp/MeshroomCache")
    echo "  cache mount OK" >> /tmp/meshroom_wrapper.log
else
    echo "  cache mount PRESKOCEN (env prazdna nebo neni adresar)" >> /tmp/meshroom_wrapper.log
fi

# Volitelne jina binarka (napr. aliceVision_texturing pro re-texturing
# vycisteneho meshe); jinak standardne meshroom_batch
BIN=/meshroom/meshroom_batch
if [[ -n "$VISTA_MESHROOM_BIN" ]]; then
    BIN="/meshroom/aliceVision/bin/$VISTA_MESHROOM_BIN"
    # prime aliceVision binarky potrebuji cestu ke knihovnam a ALICEVISION_ROOT
    # (meshroom_batch si obe nastavuje sam)
    MOUNTS+=(-e "LD_LIBRARY_PATH=/meshroom/aliceVision/lib")
    MOUNTS+=(-e "ALICEVISION_ROOT=/meshroom/aliceVision")
fi

exec docker run --rm --gpus all \
    --user "$(id -u):$(id -g)" \
    "${MOUNTS[@]}" \
    -e "HOME=/tmp" \
    "$IMAGE" \
    "$BIN" "${ARGS[@]}"
