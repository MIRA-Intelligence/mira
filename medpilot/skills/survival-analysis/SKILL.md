---
name: survival-analysis
description: End-to-end survival analysis and time-to-event modeling pipeline. Use this skill when working with censored survival data, performing time-to-event analysis, fitting Cox models, Random Survival Forests, formatting clinical datasets for survival, and evaluating survival predictions.
---

# Survival Analysis Pipeline

This skill guides the construction and iterative improvement of statistical and machine learning pipelines for survival analysis (time-to-event modeling).

## Workflow & Independent Agents

**The Iterative Cycle**: This pipeline is centered around a unified `survival_plan.yaml`. Agent 0 generates this plan. Agents 1-3 act strictly according to this plan. Agent 4 reviews the results. The user can continuously steer the analysis by modifying the plan.

### [Agent 0: Overall Planning Agent (整体设定Agent)](agents/agent_0_planning.md)
Establish the clinical hypotheses, configure the event/time variables, and define the analysis strategy.

### [Agent 1: Data Preprocessing Agent (数据预处理Agent)](agents/agent_1_data_preprocessing.md)
Perform rigorous checking of censored data, missing value imputation, and correlation analysis.

### [Agent 2: Non-parametric Analysis Agent (非参数分析Agent)](agents/agent_2_km_analysis.md)
Conduct Kaplan-Meier estimation and Log-rank tests for significant variables.

### [Agent 3: Modeling Agent (生存模型Agent)](agents/agent_3_modeling.md)
Implement Cox Proportional Hazards models, assess proportional hazards assumptions, or apply Random Survival Forests.

### [Agent 4: Evaluation Agent (模型评估Agent)](agents/agent_4_evaluation.md)
Calculate Harrell's C-index, Uno's C-index, Brier scores, and generate survival curves.

## Coding Guidelines
- Prioritize libraries like `lifelines` and `scikit-survival`.
- Ensure robust handling of right-censored data (e.g., boolean arrays or Structured Arrays).
- Provide interpretable outputs (e.g., Hazard Ratios, 95% Confidence Intervals, p-values).
