# MONAI Skill

## 1. Objective
Accelerate and standardize medical image deep learning pipelines. MONAI provides domain-optimized data reading, spatial transforms, neural network architectures, and evaluation metrics specifically built for radiology and pathology workflows.

## 2. Triggers
Use this skill when the user tasks involve:
- "Build a 3D medical image segmentation model"
- "Use MONAI for deep learning"
- "Apply 3D augmentations to NIfTI/DICOM"
- "Set up CacheDataset for fast training"
- "Evaluate Dice score or Hausdorff distance"

## 3. Core Components
- **Transforms**: Dictionary-based (`*d`) transforms for multi-modal imaging and mask alignment.
- **Datasets**: Optimized caching architectures (`CacheDataset`, `PersistentDataset`) to overcome I/O bottlenecks.
- **Networks**: Medical-specific backbones like `UNet`, `SwinUNETR`, `SegResNet`.
- **Inferers**: `SlidingWindowInferer` for patch-based evaluation of massive high-res volumes.

## 4. References & Scripts
Explore the `references/` directory for detailed guidelines on:
- `transforms.md`: Building robust preprocessing augmentations.
- `datasets.md`: Choosing the right data loader strategy.
- `networks.md`: Selecting state-of-the-art backbones.
- `losses_and_metrics.md`: Clinical validation criteria.
- `inferers.md`: Large-volume inference.
