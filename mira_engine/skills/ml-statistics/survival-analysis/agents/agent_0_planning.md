# Agent 0: Overall Planning Agent (整体设定Agent)

**Goal:** Establish the foundation for the time-to-event analysis, verify the integrity of survival data, and design a master strategy based on clinical objectives and data formats.

## Phase 1: Context & Feasibility
1. **Acquire Context**: Ask the user for:
   - **Data Path**: Where the structured tabular data (CSV/Excel) resides.
   - **Data Description**: Details about the cohort, endpoints, and variables.
   - **Clinical Hypothesis**: E.g., "Does variable X impact Overall Survival (OS)?"
2. **Data Audit**:
   - Inspect the first few rows of the dataset.
   - Verify the existence of critical time-to-event columns: "Duration/Time" and "Event/Status".
3. **Feasibility Assessment**: Evaluate if censoring is appropriately recorded (e.g., Right censorship boolean/integer arrays) and whether the sample size supports robust modeling.

## Phase 2: Core Master Plan Generation
Generate a centralized planning document `survival_plan.yaml` in the project root. This is the SINGLE SOURCE OF TRUTH for subsequent agents.

### Expected `survival_plan.yaml` Structure (Example)
```yaml
pipeline: survival-analysis
endpoints:
  time_col: "survival_time_days"
  event_col: "status_boolean"
modeling:
  type: "cox_ph" # or "rsf" (Random Survival Forest)
  alpha: 0.1     # Regularization parameter
evaluation:
  metrics: ["c_index", "brier_score"]
```
