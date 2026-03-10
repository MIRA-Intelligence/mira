# Understanding the NIfTI Format

NIfTI (Neuroimaging Informatics Technology Initiative) is the most widespread format for volume-based medical imaging in research. It consists of physical intensity data alongside metadata that anchors the image in physical space.

## Anatomy of a `nibabel` Image Object

When you call `img = nib.load('scan.nii.gz')`, you get an object with three core attributes:

1. **`img.shape`**: The spatial dimensions of the image. E.g., `(256, 256, 128)` for a 3D MRI, or `(256, 256, 128, 50)` for a 4D fMRI series.
2. **`img.affine`**: A 4x4 matrix translating voxel index coordinates `(i, j, k)` into physical coordinates `(x, y, z)` in millimeters.
3. **`img.header`**: The raw NIfTI-1 or NIfTI-2 header block containing fields like data type, intents, zooms, etc.

## Extracting the Data Array

You generally want to work with float data in a NumPy array.
```python
import nibabel as nib
import numpy as np

img = nib.load('scan.nii.gz')
data = img.get_fdata() # Returns a floating-point cast of the data
```
**Warning**: `get_fdata()` always casts to floating-point (usually `np.float64`). If you need the raw integer values (e.g., for segmentation masks), use the `np.asanyarray` wrapper on the data object:
```python
mask_data = np.asanyarray(img.dataobj) # Keeps original int type
```

## The Header and Zooms (Voxel Sizes)

The "zoom" is the physical size of the voxel in millimeters (or sometimes seconds for the 4th dimension).

```python
header = img.header
zooms = header.get_zooms()
print(f"Voxel size: {zooms} mm") # e.g., (1.0, 1.0, 2.0)
```

Modifying zooms directly in the header is possible, but usually strongly discouraged unless you physically resampled the array. NIfTI usually defines voxel size indirectly via the Affine matrix. However, you can update it if you manually construct a header:
```python
header.set_zooms((1.0, 1.0, 1.0))
```

## Creating a New NIfTI Image

When saving predictions or processed masks, you must bundle the numpy array back with an affine and a header (optional, but recommended).

```python
new_data = np.zeros_like(data)
# ... manipulate new_data ...

# Standard pattern: Create new image but copy the original affine and header
new_img = nib.Nifti1Image(new_data, img.affine, img.header)

nib.save(new_img, 'processed_scan.nii.gz')
```
