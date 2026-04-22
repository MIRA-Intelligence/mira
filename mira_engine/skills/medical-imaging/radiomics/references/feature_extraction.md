# Radiomics Feature Extraction Guidelines

## 1. Preprocessing & Resampling
Medical images often have anisotropic spacing (e.g. 1mm x 1mm x 3mm). Always resample images to isotropic spacing (e.g., 1x1x1 mm) before texture feature extraction to ensure rotational invariance of features like GLCM.
- In PyRadiomics, this is handled by `interpolator` (e.g., sitkBSpline) and `resampledPixelSpacing`.

## 2. Discretization (Intensity Binning)
- **CT Images**: Use a fixed absolute bin width (e.g., `binWidth: 25`). CT units (Hounsfield Units) are absolute.
- **MRI Images**: MRI intensity is relative. Always perform intensity normalization (e.g., Z-score scaling or N4 Bias Correction) prior to extraction, followed by a fixed bin width, or alternatively use a fixed bin count.

## 3. ROI Masks
Ensure the `Image` and the `Mask` have the exact same geometry (dimensions, spacing, origin, direction). If they do not match, PyRadiomics will throw a bounding box error. 
If data is slightly misaligned, re-sample the mask to the image's geometry using Nearest Neighbor interpolation.
