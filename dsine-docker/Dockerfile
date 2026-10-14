# DSINE (CVPR 2024) monocular surface-normal estimation — AI prior for
# denoising photogrammetric meshes (app/services/normal_prior.py).
# Repo + weights are baked in: runs offline.
FROM vista-pgsr
RUN pip install --no-cache-dir geffnet glob2
RUN git clone --depth 1 https://github.com/hugoycj/DSINE-hub.git /dsine \
    && mkdir -p /dsine/checkpoints \
    && wget -q -O /dsine/checkpoints/dsine.pt https://huggingface.co/camenduru/DSINE/resolve/main/dsine.pt \
    || python -c "import urllib.request as u; u.urlretrieve('https://huggingface.co/camenduru/DSINE/resolve/main/dsine.pt', '/dsine/checkpoints/dsine.pt')"
COPY infer_normals.py /dsine/infer_normals.py
WORKDIR /dsine
