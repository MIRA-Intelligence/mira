---
name: radiomics
description: End-to-end radiomics feature extraction and machine learning pipeline. Use this skill when configuring PyRadiomics, processing medical images and segmentations, performing feature selection, and building radiomic signatures for outcome prediction.
---

# Radiomics Pipeline

This skill provides a structured workflow for configuring, extracting, refining, and analyzing radiomic features from medical images.

## Workflow & Independent Agents

**The Iterative Cycle**: The radiomics pipeline depends on a `radiomics_plan.yaml`. Agent 0 establishes the parameters and study design. Agents 1-3 extract and process the features. Agent 4 models and evaluates the radiomic signature.

### [Agent 0: Overall Planning Agent (整体设定Agent)](agents/agent_0_planning.md)
Define the extraction parameters (e.g., bin width, interpolator, resampling spacing) and clinical endpoints.

### [Agent 1: Data Curation Agent (数据格式化Agent)](agents/agent_1_data_curation.md)
Validate image-mask spatial matching, format DICOM to NIfTI if needed, and set up index files.

### [Agent 2: Feature Extraction Agent (特征化Agent)](agents/agent_2_feature_extraction.md)
Configure PyRadiomics with YAML/JSON, execute batch extractions across the cohort, and output tabular data (CSV/DataFrames).

### [Agent 3: Feature Selection Agent (特征筛选Agent)](agents/agent_3_feature_selection.md)
Perform ICC analysis, handle high multi-collinearity, and employ techniques like LASSO, mRMR, or Recursive Feature Elimination.

### [Agent 4: Modeling & Testing Agent (建模测试Agent)](agents/agent_4_modeling_testing.md)
Train models (Logistic Regression, SVMS, etc.) on the radiomic signature, calculate AUC, plot ROC/Calibration curves, and build the Rad-score.

## Coding Guidelines
- Always generate a `pyradiomics_params.yaml` file for reproducibility.
- Strictly monitor multi-collinearity; radiomics datasets easily overfit.
- Integrate smoothly with standard standardizers (`sklearn.preprocessing.StandardScaler`).
