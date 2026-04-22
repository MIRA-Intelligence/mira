---
name: dicom2nifti
description: Python library for robustly converting DICOM medical imaging series into NIfTI (.nii.gz) format. Use this skill when you need to convert folders of DICOM files, handle slice spacing properly, calculate correct affine matrices, deal with gantry tilt, or batch process raw scanner data into standard neuroimaging formats for deep learning.
---

# Dicom2nifti

## Overview

`dicom2nifti` is a Python library specifically designed for robust conversion of DICOM medical imaging series into the standard NIfTI (`.nii` or `.nii.gz`) format. It handles complex multi-slice geometries, corrects for gantry tilt, and accurately computes image affine transformations to yield reliable inputs for 3D neuroimaging and deep learning pipelines.

## When to Use This Skill

Use this skill when working with:
- Converting raw DICOM datasets from hospital PACS into NIfTI format.
- Batch processing deeply nested folders of medical scans.
- Handling geometrically complex series (e.g., uneven spacing requiring interpolation or gantry tilt).
- Debugging issues resulting from missing slices or inconsistent coordinate metadata in DICOM files.
- Preprocessing pipelines for MONAI, PyRadiomics, or specialized medical deep learning tools.

## Installation

Install `dicom2nifti` via pip:

```bash
uv pip install dicom2nifti
```

Alternatively, it can be tested from the command line once installed:
```bash
dicom2nifti /path/to/dicom/directory /path/to/output/nifti/directory
```

## Core Workflows

### Standard Directory Conversion

The most common operation is converting a single directory of `.dcm` files representing one series into a single `.nii.gz` file.

```python
import dicom2nifti

# Read DICOM files from dicom_dir and create NIfTI file inside out_dir
dicom2nifti.convert_directory(
    dicom_directory='path/to/dicom_dir', 
    output_folder='path/to/output_dir', 
    compression=True,   # Saves as .nii.gz
    reorient=True       # Reorients to RAS+ format
)
```

### Overriding Strict Validations

`dicom2nifti` fails safely if inconsistencies are detected. You can override these safety checks if you intend to do custom interpolation down the line.

```python
import dicom2nifti.settings as settings
import dicom2nifti

# Disable strict spacing/count validation
settings.disable_validate_slice_increment()
settings.disable_validate_slicecount()

dicom2nifti.convert_directory('dicom_dir', 'output_dir')
```

## Helper Scripts

### batch_convert.py
Recursively search for DICOM series folders within a root directory and convert them into an organized NIfTI file tree.

```bash
python scripts/batch_convert.py /data/raw_dicom /data/processed_nifti
```

## Reference Materials

Detailed reference information is available in the `references/` directory:

- **conversion_logic.md**: Detailed behavioral logic of the dicom2nifti library concerning image directories and optional behaviors (like compression and reorientation).
- **troubleshooting_conversions.md**: Guide to decoding and solving common validation exceptions thrown during dataset conversions.

## Common Issues and Solutions

**Issue: `ConversionValidationError: SLICE_INCREMENT_INCONSISTENT`**
- Solution: The gap between some slices is not uniform. If this is expected, you must circumvent the check: `dicom2nifti.settings.disable_validate_slice_increment()`.

**Issue: `ConversionValidationError: MISSING_DICOM_FILES`**
- Solution: A scan slice might be physically missing based on sequence spacing geometry. Locate the corrupted scan or disable the check (`disable_validate_slicecount()`).

**Issue: "A subfolder contains multiple sub-series of geometries."**
- Solution: Ensure the `dicom_directory` passed strictly isolates one unique scanning sequence. Do not feed a root Patient directory containing T1, T2, and FLAIR simultaneously to `convert_directory` unless separated.

## Best Practices

1. **Always enable reorient**: Unless you have extremely specific registration pipelines, keeping `reorient=True` standardizes your volumes to RAS+ coordinates, minimizing orientation bugs in deep learning.
2. **Handle errors programmatically**: Never ignore `ConversionValidationError` blindly. If an error is thrown, the data is typically corrupted. Only override if you explicitly know you are imputing/interpolating later.
3. **Use compressed output**: Set `compression=True` to immediately compress outputs to `.nii.gz` to save up to 80% disk space compared to raw uncompressed NIfTI files.

## Documentation

Official dicom2nifti GitHub repository: https://github.com/icometrix/dicom2nifti
