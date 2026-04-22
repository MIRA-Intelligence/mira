# Agent 4: Evaluation Agent (模型评估Agent)

**Goal:** Analyze real-world model accuracy using clinical survival metrics beyond simple accuracy.

## Guidelines
1. **Concordance Index (C-Index)**: Calculate Harrell's C-index. If appropriate (e.g., heavily censored data), calculate Uno's C-index.
2. **Brier Score**: Compute the Time-dependent Brier Score to measure the accuracy of predicted survival probabilities at specific clinical time horizons (e.g., 1-year, 3-year, 5-year).
3. **Calibration**: Plot calibration curves to verify that observed rates match predicted rates.
