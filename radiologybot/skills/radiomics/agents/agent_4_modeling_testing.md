# Agent 4: Modeling & Testing Agent (建模测试Agent)

**Goal:** Build statistical or ML models (Rad-score) predicting the clinical outcome using the selected features.

## Guidelines
1. **Model Building**: Fit Logistic Regression, SVM, or Random Forest models. Handle class imbalances (SMOTE or class weights).
2. **Evaluation Metrics**: Generate AUC (Area Under Curve). Plot the ROC curve and the precision Calibration Curve.
3. **Rad-score Computation**: Calculate the Rad-score (linear combination of the LASSO coefficients and selected features). Evaluate distribution across groups using t-tests or Mann-Whitney.
