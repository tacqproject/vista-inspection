FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 libgl1 libglib2.0-0 libgomp1 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
