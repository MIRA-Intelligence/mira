# Agent 3: Training & Validation Agent (模型训练Agent)

**IMPORTANT**: You MUST read and strictly adhere to the `pipeline_plan.yaml`. Any parameters such as `batch_size`, `learning_rate`, `epochs`, `optimizer` must be pulled directly from this file.


**Goal:** Execute a robust, resource-efficient training loop using PyTorch and MONAI, handling 3D specific constraints and class imbalance.

## Inputs
- `dataset_*.json`: Split files from Agent 1.
- Model Definition: From Agent 2.

## Phase 1: Resource Management Strategy (VRAM)
*Address the "3D Volume VRAM Explosion" problem immediately.*

1. **Batch Size & Gradient Accumulation**:
   - **Constraint**: 3D MRI ($4 \times 128 \times 128 \times 64$) is heavy.
   - **Action**: Set physical `batch_size` to 2 or 4 (whatever fits in VRAM).
   - **Compensation**: Use **Gradient Accumulation**.
     - Accumulate gradients for $N$ steps to simulate a larger effective batch size (e.g., Target Batch 16 = Physical Batch 2 $\times$ Accumulate 8).
     - code snippet: `scaler.scale(loss / accum_iter).backward()`

2. **Mixed Precision Training (AMP)**:
   - **Mandatory**: Always use `torch.cuda.amp.autocast` and `GradScaler`.
   - **Benefit**: Reduces VRAM usage by ~40% and speeds up training.

## Phase 2: Handling Statistics & Imbalance
1. **Class Imbalance**:
   - **Sampler**: Use `WeightedRandomSampler` in the DataLoader.
     - Assign weights to samples inverse to their class frequency (calculated in Agent 1).
   - **Loss Function**:
     - *Classification*: `BCEWithLogitsLoss(pos_weight=...)` or `Focal Loss` (Monai: `FocalLoss`).
     - *Segmentation*: `DiceFocalLoss`.

2. **Overfitting Countermeasures (Small Data < 100)**:
   - **Regularization**:
     - Optimizer: `AdamW` with `weight_decay=1e-5` or `1e-4`.
     - Model: Ensure `Dropout` layers are active (rate 0.1-0.3) if architecture permits.
   - **Early Stopping**: Monitor `val_auc` (not loss). Patience ~20-50 epochs.

## Phase 3: Training Loop & Monitoring
1. **The Loop**:
   - Standard PyTorch loop iterating over `dataloader`.
   - **Validation**:
     - Run every $N$ epochs (e.g., 1 or 2).
     - **Metric**: Use **AUC (Area Under Curve)** for classification selection. Do not rely solely on Accuracy.
     - **Inference**: Use `sliding_window_inference` for dense segmentation if volumes are larger than training crop size.
   
2. **Tensorboard Logging (Rich Monitoring)**:
   - **Scalars**: Loss (Train/Val), AUC, Learning Rate.
   - **Images**:
     - Log "Input Image", "Ground Truth", and "Prediction" (middle slice of 3D volume) to visual debug.
     - **Hard Mining**: Explicitly log samples with the highest error/loss in the validation set.
   - **Figures**:
     - Real-time **ROC Curve**.
     - **Confusion Matrix** at the end of each validation epoch.

## Phase 4: Automation (5-Fold CV)
*If Agent 1 generated 5 folds, we need a unified training driver.*

1. **Script Generation**:
   - Create a `train.py` that accepts `--fold` argument.
   - Create a master shell script (`run_cross_validation.sh`) to run folds.
     - Support parallel execution if multiple GPUs are available (e.g., Fold 0 on GPU0, Fold 1 on GPU1).
     ```bash
     # Example run_cross_validation.sh
     nohup python train.py --fold 0 --gpu 0 > logs/fold0.log &
     nohup python train.py --fold 1 --gpu 1 > logs/fold1.log &
     ...
     ```

## Output
1. `train.py`: The complete training script with AMP, GradAccum, and Tensorboard logging.
2. `run_cross_validation.sh`: Helper script for batched training.
