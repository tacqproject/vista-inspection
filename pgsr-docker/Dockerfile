# PGSR (Planar-based Gaussian Splatting surface reconstruction) for VISTA —
# neural surface recon with planar / multi-view geometric priors: clean, flat
# surfaces on weakly textured concrete where depth-map MVS gets lumpy.
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-devel
ENV DEBIAN_FRONTEND=noninteractive TORCH_CUDA_ARCH_LIST="8.6" FORCE_CUDA=1
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/zju3dv/PGSR.git /PGSR
WORKDIR /PGSR
# pytorch3d is only used for quaternion_to_matrix — patched out (see patch_pytorch3d.py)
COPY patch_pytorch3d.py /tmp/
RUN python /tmp/patch_pytorch3d.py
RUN pip install --no-cache-dir open3d plyfile opencv-python-headless lpips trimesh tqdm \
    && pip install --no-cache-dir --no-build-isolation submodules/diff-plane-rasterization submodules/simple-knn
# open3d (TSDF meshing in render.py) needs EGL / X libs even headless
RUN apt-get update && apt-get install -y --no-install-recommends libegl1 libx11-6 libxext6 libxrender1 libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
