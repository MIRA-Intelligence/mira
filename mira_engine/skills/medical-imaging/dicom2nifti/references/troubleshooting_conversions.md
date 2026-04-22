# Troubleshooting Error in dicom2nifti

`dicom2nifti` is very strict by default to prevent silent corruption of medical datasets. Here are common errors and how to override them if you manually verify the data is acceptable.

## Inconsistent Slice Spacing
**Error**: `dicom2nifti.exceptions.ConversionValidationError: SLICE_INCREMENT_INCONSISTENT`
**Cause**: The distance between slices is not uniform. E.g., slice 1-10 are 2mm apart, but 11-20 are 5mm apart.
**Solution**: If you are certain this is acceptable (e.g., you will manually interpolate later), you can disable the strict check using settings:
```python
import dicom2nifti.settings as settings

# Disable strict spacing checks
settings.disable_validate_slice_increment()

# Then call convert_directory...
```
*(Note: To re-enable strict mode, use `settings.enable_validate_slice_increment()`)*

## Missing Slices
**Error**: `dicom2nifti.exceptions.ConversionValidationError: MISSING_DICOM_FILES`
**Cause**: Based on the spacing increment, it looks like a file physically belongs in a gap between two other files but is missing from the folder.
**Solution**: Disable the validation check, or locate the missing file.
```python
import dicom2nifti.settings as settings
settings.disable_validate_slicecount()
```

## Gantry Tilt Interpolation Warnings
Sometimes the conversion warns you that slices have gantry tilt and it interpolates them. If you prefer to have the raw skewed parallelepiped without interpolation (uncommon), you can change:
```python
import dicom2nifti.settings as settings
settings.disable_pydicom_read_force() # Sometimes needed for certain headers
# But for gantry tilt, dicom2nifti generally handles it automatically.
```
