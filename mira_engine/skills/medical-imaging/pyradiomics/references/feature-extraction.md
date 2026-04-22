# Feature Extraction and Classes

## The RadiomicsFeatureExtractor

The recommended way to use pyradiomics is via the `RadiomicsFeatureExtractor` module. This encapsulates all individual feature classes and provides a unified interface for passing settings and executing extractions.

### Basic Extraction Example
```python
import SimpleITK as sitk
from radiomics import featureextractor

imageName = 'path/to/image.nii.gz'
maskName = 'path/to/mask.nii.gz'

# Initialize extractor with default settings
extractor = featureextractor.RadiomicsFeatureExtractor()

# Execute extraction
result = extractor.execute(imageName, maskName)

# Results are returned as an OrderedDict
for key, value in result.items():
    if not key.startswith('diagnostics_'):
        print(f"Feature: {key}, Value: {value}")
```

## Feature Classes Breakdown

pyradiomics extracts features grouped into several distinct mathematical classes. By default, only First Order and Shape are enabled, but others can be turned on.

### 1. Shape Features (`shape` / `shape2D`)
Describe the geometry and morphological properties of the Region of Interest (ROI).
*   **Examples**: Maximum 3D Diameter, Volume, Surface Area, Sphericity, Compactness, Elongation
*   **Note**: Shape features are independent of gray level intensity and are extracted solely from the mask.

### 2. First-Order Statistics (`firstorder`)
Describe the distribution of voxel intensities within the ROI without concern for spatial relationships.
*   **Examples**: Energy, Entropy, Kurtosis, Skewness, Mean, Median, 10th/90th Percentile, Voxel Volume

### 3. Gray Level Co-occurrence Matrix (`glcm`)
Describes the second-order joint probability function of an image region. Calculates how often pairs of pixels with specific values and in a specified spatial relationship occur.
*   **Examples**: Autocorrelation, Contrast, Correlation, Joint Match, Idm, Sum Entropy

### 4. Gray Level Run Length Matrix (`glrlm`)
Quantifies gray level runs, which are the length in number of pixels, of consecutive pixels that have the same gray level value.
*   **Examples**: Short Run Emphasis (SRE), Long Run Emphasis (LRE), Run Percentage (RP)

### 5. Gray Level Size Zone Matrix (`glszm`)
Quantifies gray level zones in an image. A size zone is defined as the number of connected voxels that share the same gray level intensity.
*   **Examples**: Small Area Emphasis (SAE), Large Area Emphasis (LAE), Zone Percentage (ZP)

## Adding/Removing Feature Classes
```python
# Disable all feature classes first
extractor.disableAllFeatures()

# Enable specific classes
extractor.enableFeatureClassByName('firstorder')
extractor.enableFeatureClassByName('glcm')
```
