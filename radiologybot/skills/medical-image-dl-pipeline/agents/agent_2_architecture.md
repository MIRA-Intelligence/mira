# Agent 2: Architecture Design Agent (模型架构Agent)

**Goal:** Select and instantiate the optimal model architecture and loss function tailored to the specific clinical problem and dataset size.

## Inputs
- `pipeline_plan.yaml`: Task type (Classification, Segmentation, Detection, Registration, Synthesis) and data dimensions (2D/3D).

**Note:** Agent 2 serves as an advisory compiler. It MUST read `pipeline_plan.yaml` to extract the `architecture` and `training: loss_function` fields, and strictly implement what was requested by Agent 0 or Agent 4.


## Phase 1: Model Selection Strategy
Select the model based on the Task Type defined in the plan. Prioritize MONAI implementations.

### 1. Classification (e.g., Diagnosis, Prognosis)
*Best for: 2D/3D binary or multi-class classification.*
- **Small Dataset (e.g., < 100 samples)**:
  - **Recommendation**: `DenseNet121` or `ResNet10`/`ResNet18`.
  - **Reasoning**: Parameter efficiency is crucial to prevent overfitting.
- **Medium/Large Dataset**:
  - **Recommendation**: `EfficientNet-B0` to `B8` or `ViT` (requires pre-training).
- **MONAI Components**: `monai.networks.nets.densenet121`, `monai.networks.nets.resnet18`, `monai.networks.nets.efficientnet`.

### 2. Segmentation (e.g., Organ/Tumor Delineation)
*Best for: Voxel-wise classification.*
- **Standard Baseline**: `UNet` or `BasicUNet`.
  - Configurable strides and kernels.
- **Advanced / SOTA**:
  - `UNETR` or `SwinUNETR`: Transformer-based, best for multi-organ segmentation or complex Context.
  - `SegResNet`: Asymmetric encoder-decoder, strong winner in BraTS competitions.
  - `VNet`: Classic volumetric segmentation with residual connections.
- **MONAI Components**: `monai.networks.nets.SwinUNETR`, `monai.networks.nets.SegResNet`.

### 3. Object Detection (e.g., Nodule Detection)
*Best for: Bounding box prediction.*
- **Standard**: `RetinaNet` (Single-stage detector).
  - Includes `RetinaNet` architecture + `FocalLoss` + Box Regression Loss.
- **MONAI Components**: `monai.apps.detection.networks.retinanet.RetinaNet`.

### 4. Registration (e.g., Motion Correction, Atlas Mapping)
*Best for: Alignment/Deformation Field estimation.*
- **Affine/Rigid**: `GlobalNet` (Affine transform prediction).
- **Deformable**: `LocalNet` or `RegUNet` (Dense Displacement Field - DDF).
- **Auxiliary**: Must use `Warp` layers and losses like `LocalNormalizedCrossCorrelationLoss`, `BendingEnergyLoss`.

### 5. Generative / Synthesis (e.g., Anomaly Detection, Modality Translation)
- **Generation**: `DiffusionModelUNet`, `LatentDiffusion` (LDM).
- **Reconstruction/Anomaly**: `AutoEncoder`, `VarAutoEncoder` (VAE).
- **MONAI Components**: `monai.generative` package.

### Custom Models
- If the user requires a model not in MONAI, explicitly ask them to provide the Python file containing the PyTorch `nn.Module` definition.

## Phase 2: Loss Function Definition
Map the task to the appropriate loss function:
- **Segmentation**: `DiceCELoss` (Combines Dice and CrossEntropy, robust standard), `FocalLoss` (for class imbalance).
- **Classification**: `CrossEntropyLoss` (Multi-class), `BCEWithLogitsLoss` (Binary).
- **Reconstruction**: `MSELoss` or `L1Loss`.

## Output
1. Python code defining the Model, Loss function, and Optimizer (AdamW recommended).
2. Report reasoning for the selected model.
