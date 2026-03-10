# Survival Analysis Data Guidelines

## 1. Data Formatting (Time-to-Event)
Survival analysis evaluates the time until an event of interest occurs, factoring in missing data ("censoring"). Two critical columns are required:
1. **Time (T or Duration)**: The duration (e.g., Days, Months, or Years) from diagnosis/treatment until the event occurred, OR until the patient was lost to follow-up (censoring).
2. **Event (E or Status)**: A binary integer indicating whether the event occurred at time T. 
   - `1` = Event occurred (e.g., Death, Progression).
   - `0` = Censored (e.g., Patient survived until last follow-up, or was lost).

## 2. Kaplan-Meier & Log-Rank 
- Kaplan-Meier estimates survival over time.
- The Log-Rank test compares two strictly categorical populations (e.g., Male vs Female, Treatment vs Control). 
- If using numerical data (like a model's predicted risk score), you must split the cohort (e.g., split at median) into High-Risk and Low-Risk groups before drawing KM curves.

## 3. Cox Proportional Hazards
- Computes **Hazard Ratios (HR)**. HR > 1 means increased risk, HR < 1 means protective.
- Evaluated via the **Concordance Index (C-index)**. A C-index of `0.5` represents random chance; `1.0` is perfect prediction accuracy.
