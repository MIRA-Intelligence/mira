# Configuration and Parameter Tuning

pyradiomics behaviour is highly customizable. The best practice for keeping experiments reproducible is storing extraction parameters in a YAML configuration file.

## Essential Settings

### 1. Discretization (`binWidth` vs `binCount`)
Before computing texture matrices (GLCM, GLRLM, etc.), image intensities must be discretized.
*   **`binWidth`** (Recommended): Specifies the width of the bins. This ensures that the relationship between pixel intensities and actual physical/biological meaning remains consistent (critical for CT scans in Hounsfield Units, HU).
    *   *CT Example*: `binWidth: 25` (Standard for CT radiomics).
*   **`binCount`**: Specifies a fixed number of bins. Often preferred for MRI, where absolute intensity values are not strictly standardized.

### 2. Resampling (`resampledPixelSpacing`)
Medical images vary in voxel spacing. Resampling ensures that texture features are comparable across different scans.
*   **`resampledPixelSpacing: [1, 1, 1]`**: Resamples the image and mask to isotropic 1x1x1 mm resolution.
*   **`interpolator`**: Determines how voxel values are interpolated.
    *   Images: `sitkBSpline` (default)
    *   Masks: Always forced to `sitkNearestNeighbor` by pyradiomics internally to preserve categorical label values.

### 3. Normalization (Primarily for MRI)
Because MRI intensities are relative, normalization is highly recommended.
*   **`normalize: true`**
*   **`normalizeScale: 100`**: Scales the normalized values.

## Modality-Specific Recommendations

### CT (Computed Tomography)
*   Use `binWidth` (usually 25).
*   Do **NOT** use normalization.

### MRI (Magnetic Resonance Imaging)
*   Use Normalization.
*   Consider using `binCount` or strict Z-score normalization followed by `binWidth`.

## Example YAML Configurations

### CT Parameter YAML
```yaml
imageType:
  Original: {}
  LoG:
    sigma: [1.0, 3.0, 5.0]

featureClass:
  shape:
  firstorder:
  glcm:

setting:
  binWidth: 25
  resampledPixelSpacing: [1, 1, 1]
  interpolator: 'sitkBSpline'
```

### MRI Parameter YAML
```yaml
imageType:
  Original: {}

featureClass:
  shape:
  firstorder:
  glcm:

setting:
  normalize: true
  normalizeScale: 100
  resampledPixelSpacing: [1.5, 1.5, 1.5]
  binWidth: 5
```
