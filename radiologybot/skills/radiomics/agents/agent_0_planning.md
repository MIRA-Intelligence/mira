# Agent 0: Overall Planning Agent (整体设定Agent)

**Goal:** Establish the foundation, inspect image/mask directories, and design a feature extraction strategy.

## Phase 1: Context & Feasibility
1. **Acquire Context**: 
   - **Image Path**: Folders containing raw data.
   - **Mask Path**: Folders containing segmentations.
   - **Data Modality**: e.g., CT, T1-MRI, T2-MRI, PET.
2. **Setup Configurations**: Establish whether 2D or 3D extraction is required, and what physical voxel spacing to standardize around.

## Phase 2: Core Master Plan Generation
Create `radiomics_plan.yaml`.

### Expected `radiomics_plan.yaml` Structure (Example)
```yaml
pipeline: radiomics
modality: "CT"
paths:
  images: "./data/images"
  masks: "./data/masks"
extraction:
  resample_spacing: [1, 1, 1]
  bin_width: 25   # Specific to CT. MRI might need 5 or dynamic.
selection:
  icc_threshold: 0.75
  variance_threshold: 0.1
```
