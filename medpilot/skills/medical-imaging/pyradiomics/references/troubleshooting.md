# Post-Extraction and Troubleshooting

When integrating `pyradiomics` into larger scripts, you will inevitably encounter geometry/mask mismatches. pyradiomics strictly enforces alignment between the Image and the Mask.

## Common Errors & Solutions

### 1. "Image/Mask geometry mismatch"
**Error**: `Exception: Image and Mask geometry mismatch. Mismatch in Size/Spacing/Direction/Origin.`
**Cause**: The mask was generated with different physical properties than the underlying image.
**Solution**: Use SimpleITK to copy the information from the image to the mask.

```python
import SimpleITK as sitk

image = sitk.ReadImage('image.nii.gz')
mask = sitk.ReadImage('mask.nii.gz')

# Force mask geometry to match image geometry
mask.CopyInformation(image)

if image.GetSize() != mask.GetSize():
    raise ValueError("Sizes still do not match. Manual resampling required.")
```

### 2. "Bounding box of ROI is larger than image"
**Error**: `Exception: Bounding box of ROI is larger than image`
**Cause**: Often occurs when applying `resampledPixelSpacing`. The resampling grid bounds might accidentally shift slightly outside the original mask bounds.
**Solution**: Enable the `padDistance` setting in pyradiomics to add a buffer of voxels during resampling.
```yaml
setting:
  padDistance: 10 # Adds 10 voxels padding
```

### 3. "No valid voxels found in the mask"
**Error**: Warning or Error that no voxels match the label value.
**Cause**: The extraction label doesn't exist in the mask.
**Solution**: Check the `label` parameter for the extractor (Default is `1`).
```python
# If the tumor mask is defined by value 2:
extractor.settings['label'] = 2
```

## Batch Processing Tip
pyradiomics provides a built-in batch processing script `pyradiomics <batch-file.csv> -o <output.csv> -p <params.yaml>`. However, building a custom Python loop using `pandas` and `multiprocessing` is often preferred for research pipelines because it allows better error capture and inline pre-processing (like geometry correction).
