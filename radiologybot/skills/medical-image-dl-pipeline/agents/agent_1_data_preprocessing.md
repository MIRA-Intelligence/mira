# Agent 1: Data Preprocessing Agent (数据预处理Agent)

**Goal:** Perform robust data splitting and design a MONAI-based preprocessing pipeline tailored to the data characteristics.

## Inputs
- `dataset.json`: The index of raw data.
- `pipeline_plan.json`: The strategy defined by Agent 0 (spacing, intensity stats, etc.).

## Phase 1: Data Splitting Strategy
*Before any processing, partition the dataset to prevent leakage and ensure fair evaluation.*

1. **Patient-Level Splitting (Crucial)**:
   - Identify unique Patient IDs.
   - **Constraint**: All images/slices from the same patient MUST belong to the same fold/split. Never split a single patient's data across Train and Test.

2. **Stratification (for Classification)**:
   - Calculate the positive/negative ratio (or class distribution) at the *patient level*.
   - Perform stratified sampling to ensure the Train/Val/Test sets have similar class distributions to the overall population.

3. **Cross-Validation vs. Hold-out**:
   - **Method Selection**:
     - *Small Dataset (< 120 patients)*: Recommend **5-Fold Cross-Validation**.
     - *Large Dataset*: Recommend standard Hold-out split (e.g., 70% Train, 10% Val, 20% Test).
   - **Output**: Generate `dataset_0.json`, `dataset_1.json`... (or a single JSON with explicit `fold` keys) defining the splits.

## Phase 1.5: Visual Quality Control & Policy Adjustment

*Before finalizing the preprocessing pipeline, physically inspect the data to catch artifacts like MRI bias fields or variable imaging settings.*



1. **Sample & Plot (Scripting)**: Randomly select ~10 cases from the dataset. Write a short Python script using `matplotlib` to plot the middle slices (e.g., central Axial, Coronal, or Sagittal slices) across ALL input modalities for these subjects.

2. **Save**: Save these compiled plots as `.png` files in a dedicated `qc_snapshots/` folder.

3. **Vision Analysis**: You MUST read and analyze these saved PNG images using vision capabilities to visually inspect the data characteristics.

4. **Policy Adjustments based on visual evidence**:

   - **N4 Bias Field Correction**: If you observe low-frequency intensity gradient/inhomogeneity (especially common in uncorrected MRI), explicitly add `N4BiasFieldCorrection` to the preprocessing pipeline.

   - **Cropping/Foreground Extraction**: Check if there is excessive empty background around the target anatomy. Formulate a plan for `CropForegroundd` or Masking if present.

   - **Dynamic Augmentations**: Select appropriate data augmentation based on visual evidence. For example, if contrast is highly variable, enforce `RandAdjustContrastd`/`RandHistogramShiftd`; if there is heavy noise, apply `RandGaussianNoised`.



## Phase 2: Core Preprocessing Steps
Define a MONAI `Compose` pipeline incorporating these steps. **Critical:** Differentiate between `train_transforms` (with augmentation) and `val_transforms` (clean).

1. **Common Preprocessing (All Splits)**:
   - **Reorientation**: Unify to `RAS` or `LPS` (`Orientationd`).
   - **Resampling**: Target Spacing from `pipeline_plan.json`.
     - *Images*: Bilinear/Bicubic.
     - *Labels*: Nearest Neighbor.
   - **Intensity Normalization**:
     - *CT*: Clip (`ScaleIntensityRanged`) and normalize to [0, 1].
     - *MRI*: Z-score (`NormalizeIntensityd`) or scale to [0, 1].
   - **Channel Stacking**: Ensure `(C, D, H, W)` format (`EnsureChannelFirstd`).

2. **Data Augmentation (Train Split ONLY)**:
   - Select physiologically plausible transforms.
   - **Valid**: Random Rotate (small angles), Zoom, Intensity Shift, Gaussian Noise, Spatial Crop.
   - **Invalid**: Vertical Flip (for non-symmetric anatomy like brain), Extreme Shear.

3. **Code Reuse Strategy**:
   - Use a single `Monai.data.Dataset` class.
   - Pass different transform chains (`train_transforms` vs `val_transforms`) to the Dataset instance to avoid code duplication.

## Phase 3: Implementation Strategy (Online vs Offline)
Decide on usage of `CacheDataset` vs Persistent Dataset vs standard `Dataset` based on data size and task.

### Strategy A: 2D Inputs / Slices
- **Method**: Online processing.
- Implementation: Use standard `monai.data.Dataset` or `CacheDataset`.
- Resampling is fast enough on-the-fly.

### Strategy B: 3D Volumes (Heavy)
- **Method**: Hybrid or Offline.
- **Critical Step - Offline Resampling Script**:
  - If volumes are large (e.g., 512x512x500 CTs), create a standalone Python script to pre-resample all data to the target spacing *before* training.
  - Save these "pre-cached" volumes to disk.
  - Update `dataset.json` to point to these new files.
- **Training**: The DataLoader then only handles cropping/augmentation, skipping heavy resampling.

## Output
1. The JSON file(s) defining the Train/Val/Test splits (checking for patient leakage).
2. Python code defining `train_transforms` and `val_transforms`.
3. If Strategy B is chosen, the `pre_resample.py` script.
