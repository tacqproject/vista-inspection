# VISTA automatic defect detection on inspection photos (GPU).
# Crack segmentation: SegFormer fine-tuned on a crack dataset (weights baked in).
FROM vista-dsine
RUN pip install --no-cache-dir "transformers==4.46.3" scikit-image
RUN python -c "from transformers import SegformerForSemanticSegmentation as S; S.from_pretrained('varcoder/segformer-b4-crack-segmentation-dataset').save_pretrained('/models/crack_segformer')" && chmod -R a+rX /models
COPY detect_defects.py /defects/detect_defects.py
WORKDIR /defects
