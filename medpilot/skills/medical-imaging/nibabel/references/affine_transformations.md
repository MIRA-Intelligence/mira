# Affine Matrix and Spatial Orientations

The affine matrix is at the heart of `nibabel`. It maps voxel coordinates (the row, column, slice indices of your NumPy array) into the physical "scanner space" coordinates (real-world millimeters).

## Understanding the Affine Matrix

The affine is a $4 \times 4$ transformation matrix.
It handles:
1. **Translation**: Where the origin is located.
2. **Scaling**: The voxel spacing in each dimension (often related to zooms).
3. **Rotation/Shear**: The orientation of the patient relative to the scanner (e.g., patient tilt).

### Reading the affine
```python
import nibabel as nib
img = nib.load('image.nii.gz')
affine = img.affine
print(affine)
```

## Image Orientation (RAS+ vs LPS+)

Medical images can be acquired in various orientations depending on patient positioning (e.g., Supine vs Prone) and scanner manufacturer preferences.
*   **RAS+ (Right, Anterior, Superior)**: The standard orientation in NIfTI format. Moving along the positive axes moves you towards the Right, Anterior, or Superior parts of the body.
*   **LPS+ (Left, Posterior, Superior)**: Typical in DICOM.

It is a common ML pipeline step to enforce **RAS+ canonical orientation** to guarantee uniform array shapes.

### Reorienting to Canonical RAS+

```python
import nibabel as nib
import nibabel.orientations as nio

img = nib.load('image.nii.gz')

# Determine original orientation
orig_ornt = nio.io_orientation(img.affine)

# Define target canonical orientation (RAS+)
targ_ornt = nio.axcodes2ornt(('R', 'A', 'S'))

# Calculate the transformation from original to target
transform = nio.ornt_transform(orig_ornt, targ_ornt)

# Apply transformation to data array
new_data = nio.apply_orientation(img.get_fdata(), transform)

# Compute new affine
new_affine = nio.inv_ornt_aff(transform, img.shape)
new_affine = img.affine.dot(new_affine)

# Save standardized image
reoriented_img = nib.Nifti1Image(new_data, new_affine, img.header)
nib.save(reoriented_img, 'image_ras.nii.gz')
```
*Alternatively*, since nibabel version 2.4, there is a built-in shortcut:
```python
img = nib.load('image.nii.gz')
canonical_img = nib.as_closest_canonical(img)
nib.save(canonical_img, 'image_ras.nii.gz')
```
