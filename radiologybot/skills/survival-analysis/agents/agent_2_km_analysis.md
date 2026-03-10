# Agent 2: Non-parametric Analysis Agent (非参数分析Agent)

**Goal:** Estimate survival probabilities and conduct univariate statistical testing.

## Guidelines
1. **Kaplan-Meier Estimator**: Compute Kaplan-Meier curves for the overall cohort and across sub-cohorts (e.g., treatment A vs B, or high-risk vs low-risk groups).
2. **Log-Rank Testing**: Calculate the statistical significance of survival difference between groups using the log-rank test. Ensure you output the exact `p-value`.
3. **Visualization**: Generate survival plots. Ensure the x-axis (Time) is labeled with the exact clinical unit, and include an "At Risk" table below the x-axis if requested.
