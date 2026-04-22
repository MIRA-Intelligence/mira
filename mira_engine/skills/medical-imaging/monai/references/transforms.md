# MONAI Transforms

## Dictionary vs. Array Transforms
Always default to **Dictionary Transforms** (ending in `d` or `D`, e.g., `LoadImaged`, `Spacingd`). They allow simultaneous and deterministic application of spatial and intensity augmentations to pairs of `{"image": img_path, "label": mask_path}`.

## Essential Pipeline
1. **Load**: `LoadImaged(keys=["image", "label"])`
2. **Channel Format**: `EnsureChannelFirstd(keys=["image", "label"])`
3. **Spacing (Crucial)**: Resample to uniform voxel size.
   `Spacingd(keys=["image", "label"], pixdim=(1.0, 1.0, 1.0), mode=("bilinear", "nearest"))`
4. **Orientation**: Standardize to RAS+ or LPS+.
   `Orientationd(keys=["image", "label"], axcodes="RAS")`
5. **Intensity Normalization**: `ScaleIntensityRanged` (CT) or `NormalizeIntensityd` (MRI).
6. **Cropping**: `RandCropByPosNegLabeld` to ensure pathological regions are properly sampled during training.
