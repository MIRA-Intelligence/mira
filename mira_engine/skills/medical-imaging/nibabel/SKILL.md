---
name: nibabel
description: Python library for reading and writing medical/neuroimaging data formats, particularly NIfTI (.nii, .nii.gz). Use this skill when the user wants to load medical imaging arrays, manipulate NIfTI headers (e.g., affine matrices, voxel sizes), modify spatial orientations (RAS/LPS), or save processed NumPy arrays back to standard neuroimaging formats for further analysis.
---

# Nibabel

## Overview

Nibabel (`nibabel`) is the primary Python library to read and write neuroimaging and medical imaging data formats, most notably NIfTI (`.nii` and `.nii.gz`). Instead of dealing with nested DICOM directories, researchers use Nibabel to ingest single file multi-dimensional NumPy arrays and read their spatial header definitions (like Voxel sizes and orientation coordinates/Affines).

## When to Use This Skill

Use this skill when working with:
- Volumetric NIfTI images (`.nii`, `.nii.gz`) for deep learning and medical pipelines.
- Accessing the underlying NumPy arrays of 3D/4D scans.
- Manipulating spatial orientations or reading the underlying Affine (`4x4`) coordinate mapping matrices.
- Creating brand new NIfTI files from synthetic arrays or post-network probability predictions.
- Modifying image header metadata (like Voxel dimensions and zoom).

## Installation

Install nibabel via pip:

```bash
uv pip install nibabel
```

## Core Workflows

### Loading NIfTI Arrays

Load an image, inspect its coordinate space, and pull the raw numpy pixel data:

```python
import nibabel as nib
import numpy as np

# Load a NIfTI file
img = nib.load('scan_t1.nii.gz')

# Get the affine matrix (coordinate space)
affine = img.affine
print(f"Affine Matrix:\n{affine}")

# Access the multi-dimensional numpy array directly
data = img.get_fdata()
print(f"Volume Shape: {data.shape}")
```

### Saving an Array back to NIfTI

If you infer a segmentation mask out of a neural network (as a numpy array), save it using the same spatial referencing as the source.

```python
import nibabel as nib
import numpy as np

# Suppose 'predicted_mask' is a binary numpy volume, and 'img' is your source nibabel object
predicted_mask = np.zeros(img.shape)

# Create a new Nifti1Image paired with the original spatial Affine
new_img = nib.Nifti1Image(predicted_mask.astype(np.float32), affine=img.affine)

# Save to disk
nib.save(new_img, 'prediction.nii.gz')
```

## Helper Scripts

### basic_operations.py
Provides ready-to-use functions for inspecting NIfTI volume shapes, reading headers, and calculating the zoom/voxel sizes directly from an affine matrix without boilerplate.

```bash
python scripts/basic_operations.py check_volume.nii.gz
```

## Reference Materials

Detailed reference information is available in the `references/` directory:

- **nifti_format.md**: Breakdown of core NIfTI objects (`.get_fdata()`, `.header`), precision typing, and memory considerations.
- **affine_transformations.md**: Explanation of orientation arrays (RAS+ standard), coordinate mapping, and Voxel size calculations from Affines.

## Common Issues and Solutions

**Issue: Out of Memory when calling `.get_fdata()`**
- Solution: `get_fdata()` converts the payload to `float64` by default. If RAM is limited, use `img.dataobj` directly or `np.asanyarray(img.dataobj)` to retain the native datatypes (e.g., `uint8`, `int16`).

**Issue: Geometric misalignment between Image and Label (Masks off by 90/180 degrees)**
- Solution: Always use the exact affine of the source image when saving the mask: `nib.Nifti1Image(mask, original_image.affine)`. Do NOT pass `np.eye(4)` recklessly.

**Issue: Changing orientation (e.g., changing internal LPS to RAS+)**
- Solution: Do not just transpose the numpy array manually. Utilize Nibabel's orientation utilities: `nib.orientations.ornt_transform`.

## Best Practices

1. **Retain Affine Linkages:** Any mask or prediction output corresponding to a specific input must absolutely use the input image's `.affine`. This anchors the predictions perfectly with the underlying patient anatomy tools.
2. **Watch the Extension:** Passing `.nii` implies uncompressed formats. Append `.nii.gz` to ensure compression on output seamlessly.
3. **Validate Shapes:** 3D images have lengths of 3 `(X, Y, Z)`, while functional MRI (fMRI) or Dynamic Contrast Enhanced scans might be 4D `(X, Y, Z, Time)`. Use `header.get_data_shape()` to be safe without loading to memory.

## Documentation

Official Nibabel Documentation: https://nipy.org/nibabel/
- Getting Started: https://nipy.org/nibabel/gettingstarted.html
- The NIfTI format: https://nipy.org/nibabel/nifti_images.html
- Coordinates/Affines: https://nipy.org/nibabel/coordinate_systems.html
