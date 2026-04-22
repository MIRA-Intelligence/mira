# Sliding Window Inferer

## The Problem
3D neural networks consume massive amounts of VRAM. You cannot fit an entire 512x512x512 CT scan into a GPU to retrieve a segmentation.

## The Solution
`SlidingWindowInferer` sweeps a predefined FOI (Field of View/ROI) across the volumetric tensor, predicting patches, and stitches them back together seamlessly. It even supports Gaussian blending for overlapping patches to prevent border artifacts.

## Usage
```python
from monai.inferers import sliding_window_inference

# Inside validation loop:
val_outputs = sliding_window_inference(
    inputs=val_images,
    roi_size=(96, 96, 96), # MUST match training patch size
    sw_batch_size=4,       # How many windows to process at once
    predictor=model,
    overlap=0.5            # Blend patch overlaps
)
```
