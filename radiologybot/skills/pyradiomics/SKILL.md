---
name: pyradiomics
description: Comprehensive toolkit for extracting radiomics features from medical images using pyradiomics. Use this skill when working with feature extraction from CT, MRI, PET, or other medical imaging modalities, configuring feature extractors (e.g., bin width, resampling, filtering), handling shape, first-order, and texture features (GLCM, GLRLM, GLSZM, GLDM, NGTDM), or integrating radiomics into machine learning pipelines.
---

# pyradiomics: Radiomics Feature Extraction Toolkit

## Overview
PyRadiomics is an open-source Python package for the extraction of radiomic features from medical imaging data. It provides a standardized and reproducible framework for computing shape, first-order (intensity), and texture features (such as GLCM, GLRLM, GLSZM, GLDM, and NGTDM). By applying customizable image filters and preprocessing steps (like resampling and normalization), it enables researchers to quantify tumor phenotypes and extract vast sets of quantitative imaging biomarkers for machine learning pipelines.

## When to Use This Skill
- Extracting quantitative radiomic features from medical images and their corresponding mask/segmentation files.
- Configuring feature extraction pipelines (e.g., defining bin widths, voxel resampling sizes, and spatial normalization).
- Applying pre-processing image filters (e.g., Wavelet, LoG, LBP) prior to feature calculation.
- Batch processing large cohorts of medical imaging studies to generate tabular feature data (CSV/DataFrames) for downstream statistical or machine learning tasks.
- Integrating radiomics logic with standard pipelines like `scikit-learn` or `scikit-survival`.

## Core Capabilities

### Feature Classes
- **Shape**: 2D and 3D geometric properties of the ROI.
- **First-Order**: Voxel intensity distributions within the ROI.
- **Texture Matrices**: 
  - Gray Level Co-occurrence Matrix (GLCM)
  - Gray Level Run Length Matrix (GLRLM)
  - Gray Level Size Zone Matrix (GLSZM)
  - Gray Level Dependence Matrix (GLDM)
  - Neighborhood Gray Tone Difference Matrix (NGTDM)

### Image Filters
- **Wavelet**: Directional frequency filtering.
- **LoG (Laplacian of Gaussian)**: Edge and blob enhancement at different sigma values.
- **LBP 2D/3D (Local Binary Pattern)**: Texture analysis.
- **Gradient, Square, SquareRoot, Logarithm, Exponential**: Mathematical transformations of intensities.

### Configuration Management
- Using custom parameter files ( YAML/JSON ) to define extraction settings robustly (e.g., `binWidth`, `interpolator`, `resampledPixelSpacing`).

## Typical Workflows

### 1. Basic Single Image-Mask Extraction
Setting up an extractor and processing a single case.
```python
from radiomics import featureextractor
import SimpleITK as sitk

image_path = "path/to/image.nii.gz"
mask_path = "path/to/mask.nii.gz"

# Initialize extractor with default settings
extractor = featureextractor.RadiomicsFeatureExtractor()

# Execute extraction
result = extractor.execute(image_path, mask_path)

for key, value in result.items():
    print(f"{key}: {value}")
```

### 2. Parameter File Configuration
Creating reproducible workflows using YAML parameters.
```python
import os
from radiomics import featureextractor

params_file = "path/to/Params.yaml"
# Create extractor from parameter file
extractor = featureextractor.RadiomicsFeatureExtractor(params_file)

image_path = "path/to/image.nii.gz"
mask_path = "path/to/mask.nii.gz"
result = extractor.execute(image_path, mask_path)
```

### 3. Batch Processing with pandas
Extracting features over a dataset to train ML models.
```python
import pandas as pd
from radiomics import featureextractor

cases = [{"image": "img1.nii", "mask": "mask1.nii"}, {"image": "img2.nii", "mask": "mask2.nii"}]
extractor = featureextractor.RadiomicsFeatureExtractor("Params.yaml")

results_list = []
for case in cases:
    result = extractor.execute(case["image"], case["mask"])
    results_list.append(result)

df = pd.DataFrame(results_list)
df.to_csv("radiomics_features.csv", index=False)
```

## Integration with machine learning
Radiomics features extracted directly map to downstream analysis libraries.
- The output from pyradiomics can often be directly injected into a `pandas.DataFrame`.
- From there, standardization tools like `sklearn.preprocessing.StandardScaler` can be applied.
- The standardized tabular data works smoothly with `scikit-survival` or `scikit-learn` algorithms.

## Best Practices
- **Standardize Acquisition Parameters**: Variability in voxel size, slice thickness, and reconstruction kernels heavily impacts feature robustness.
- **Always Resample**: Use pyradiomics' native resampling (configure `resampledPixelSpacing` and `interpolator`) to achieve isotropic voxel sizes before extracting texture features.
- **Tune Bin Width**: Discretization is critical. A `binWidth` between 5 and 25 is typical for CT, but setting this optimally requires understanding your modality's intensity spread (e.g., HU for CT).
- **Use Parameter Files**: Always configure your pipeline using `.yaml` parameter files for scientific reproducibility rather than hard-coding settings.
- **Handle Diagnostics**: Monitor the `diagnostics_` features appended by pyradiomics to check for execution warnings or metadata inconsistencies.

## Common Pitfalls to Avoid
- **Mask Mismatches**: The image and mask geometry (Origin, Spacing, Direction) must match exactly, or PyRadiomics will throw a `Bounding box` or `Dimension` error.
- **Normalization on CT**: Do not normalize Hounsfield Units (CT data) as their absolute values carry physical meaning. Normalization is mainly required for MRI.
- **Overfitting**: Extracting all features + all filters can yield thousands of variables. If your sample size is small, you *will* overfit. Apply strong feature selection.

## Reference Files
If creating radiomics projects, refer to the included codebase templates and parameter configurations:
- `references/example_params.yaml`: Standard configuration template for CT/MRI.
- `references/batch_extractor.py`: Boilerplate for multiprocessing over large cohorts.

## Additional Resources
- [PyRadiomics Documentation](https://pyradiomics.readthedocs.io/)
- [IBSI Standards](https://arxiv.org/abs/1612.07003) (Image Biomarker Standardisation Initiative)

## Quick Reference: Key Imports

```python
# Main Extractor
from radiomics import featureextractor

# Logging Control
import logging
from radiomics import setVerbosity

# Image handling for pyradiomics
import SimpleITK as sitk
```
