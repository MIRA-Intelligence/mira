# MONAI Losses and Metrics

## Losses
- **DiceCELoss**: The gold standard for medical segmentation. Combines Softmax/Cross-Entropy (for general structured classifying) and Dice loss (for addressing class imbalances).
  - Use `include_background=False` if background is mostly empty space.
  - Use `softmax=True` if the network outputs raw logits.
- **FocalLoss**: Excellent for extremely imbalanced targets (e.g., small lesions).
- **TverskyLoss**: A variation of focal/dice optimized for balancing false positives and false negatives.

## Metrics
Metrics must be calculated carefully. You usually need `AsDiscrete(argmax=True, to_onehot=num_classes)` before computing.
- **DiceMetric**: Computes multi-class Dice overlay.
- **HausdorffDistanceMetric**: Calculates 95% HD (set `percentile=95`). Crucial clinical requirement to evaluate boundary fidelity.
