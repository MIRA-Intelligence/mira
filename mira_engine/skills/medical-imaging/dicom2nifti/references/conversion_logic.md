# DICOM to NIfTI Conversion Logic

When transforming raw DICOM files to NIfTI, it is generally never safe to just load 2D images and stack them into a 3D NumPy array without geometric math. `dicom2nifti` handles this abstraction safely.

## Standard Usage in Python

```python
import dicom2nifti
import os

dicom_directory = '/path/to/dicom/series'
output_folder = '/path/to/output/nifti'

# It outputs either a single file inside the output_folder or multiple
# depending on what is found inside the dicom_directory.
dicom2nifti.convert_directory(dicom_directory, output_folder, compression=True, reorient=True)
```

## Why Stacking Fails (Why dicom2nifti exists)

1. **Slice Sorting**: Filenames (e.g. `IMA001.dcm`) do NOT guarantee anatomical ordering. `dicom2nifti` parses the DICOM `ImagePositionPatient` tag to correctly order slices in physical space.
2. **Missing Slices**: `dicom2nifti` parses the difference between consecutive `ImagePositionPatient` tags. If it detects a jump (> 5% discrepancy), it will throw an error to prevent you from using corrupted voxel data in your convolutional networks.
3. **Gantry Tilt**: CT scanners can tilt the gantry angle, causing slices to be acquired as parallelepipeds instead of a pure rectangular cuboids. Stacking these creates diagonal sheer. `dicom2nifti` detects this and interpolates the volume to an orthogonal grid.
4. **Resampling / Reorientation**: By default (`reorient=True`), the library attempts to align the NIfTI outputs into the standard neuroimaging coordinate system (RAS+), which prevents issues where left-right is flipped when loaded using `nibabel`.

## Memory Management
If memory usage is a problem for large volumes, `dicom2nifti` settings can be adjusted, but normally passing the directory paths directly keeps overhead manageable.
