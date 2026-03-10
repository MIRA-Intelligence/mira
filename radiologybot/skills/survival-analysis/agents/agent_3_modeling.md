# Agent 3: Modeling Agent (生存模型Agent)

**Goal:** Fit survival models using parameters defined in `survival_plan.yaml` and assess key statistical assumptions.

## Phase 1: Fit Model
1. If `type: cox_ph`, fit a Cox Proportional Hazards model using `lifelines` or `scikit-survival`.
2. If `type: rsf`, fit a Random Survival Forest.

## Phase 2: Statistical Verification
1. **Proportional Hazards (PH) Assumption**: For Cox models, ALWAYS check the Schoenfeld residuals (using `check_assumptions` in lifelines or custom statistical tests).
2. **Feature Importance / Hazard Ratios**: Extract the Hazard Ratio (exp(coef)) and the 95% Confidence Interval for each feature.
