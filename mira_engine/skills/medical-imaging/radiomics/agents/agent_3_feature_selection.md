# Agent 3: Feature Selection Agent (特征筛选Agent)

**Goal:** Filter the noise. Radiomics generates hundreds of features; highly collinear ones must be eliminated.

## Guidelines
1. **Robustness (ICC)**: If test-retest scans are available, drop features with ICC < 0.75.
2. **Standardization**: Implement `sklearn.preprocessing.StandardScaler`.
3. **Collinearity Filter**: Drop features using Pearson/Spearman correlation (e.g., if correlation > 0.85, drop one).
4. **Advanced Selection**: Use algorithms like LASSO regression (L1 regularization), mRMR, or Recursive Feature Elimination. Keep only an essential subset (e.g., 5-15 features).
