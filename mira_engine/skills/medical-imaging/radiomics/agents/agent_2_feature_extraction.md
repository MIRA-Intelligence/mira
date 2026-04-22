# Agent 2: Feature Extraction Agent (特征化Agent)

**Goal:** Execute batch extractions using PyRadiomics.

## Guidelines
1. **Parameter File Generation**: Create a robust `pyradiomics_params.yaml` config (e.g., enable LoG/Wavelet filters, set up shape/firstorder/glcm).
2. **Execution Logging**: Use `radiomics.setVerbosity` to suppress standard INFO spam while extracting. 
3. **Dataframe Consolidation**: Output extraction results to a `features.csv`.
