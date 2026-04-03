# Agent 1: Data Curation Agent (数据格式化Agent)

**Goal:** Format the clinical and image data. Create file indices bridging images to their segmentations.

## Guidelines
1. **File Matching**: Cross-reference image filenames with mask filenames.
2. **Metadata Consistency**: Check SimpleITK headers. Origin, Spacing, and Direction must be identical between image and mask; otherwise pyradiomics will panic with dimension mis-matches.
3. **Generate Dataset JSON**: Produce a `dataset.json` holding dictionaries of `{"image": "path", "mask": "path"}`.
