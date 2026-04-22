# Agent 1: Data Preprocessing Agent (数据预处理Agent)

**Goal:** Format the clinical and feature data to strictly adhere to standard survival analysis libraries, handling missing data and data leakage.

## Guidelines
1. **Imputation**: Handle missing values in covariates judiciously (mean/median for continuous, mode for categorical, or advanced imputation like KNN).
2. **Formatting for scikit-survival**: Convert the target variables into a structured array of tuples (boolean/bool, float/int).
   ```python
   # Example structural array conversion required by scikit-survival
   y = np.array([(bool(status), time) for status, time in zip(event_col, time_col)], dtype=[('Status', '?'), ('Time', '<f8')])
   ```
3. **Correlation Analysis**: Identify highly correlated covariates to prevent multicollinearity in Cox models (e.g., drop features with $>0.8$ correlation).
