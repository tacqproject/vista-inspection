# OpenMVS 2.3 (CPU build) for VISTA — densify / mesh / photometric RefineMesh / texture.
# The official Docker Hub image is OpenMVS 1.1 from 2019 (its COLMAP import crashes).
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git cmake build-essential ca-certificates \
      libpng-dev libjpeg-dev libtiff-dev libglu1-mesa-dev libglew-dev libglfw3-dev \
      libboost-iostreams-dev libboost-program-options-dev libboost-system-dev \
      libboost-serialization-dev libboost-python-dev \
      libopencv-dev libcgal-dev libeigen3-dev python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /vcglib
RUN git clone --depth 1 --branch v2.3.0 https://github.com/cdcseacave/openMVS.git /openMVS \
    && mkdir /openMVS_build && cd /openMVS_build \
    && cmake /openMVS -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/vcglib \
         -DOpenMVS_USE_CUDA=OFF -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF \
    && make -j"$(nproc)" && make install \
    && rm -rf /openMVS_build
ENV PATH=/usr/local/bin/OpenMVS:$PATH
