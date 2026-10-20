# Medical Image Analysis — Research Article

## Sources and verification

- Official author guide: [MedIA Guide for Authors](https://www.sciencedirect.com/journal/medical-image-analysis/publish/guide-for-authors)
- Official journal: [Medical Image Analysis](https://www.sciencedirect.com/journal/medical-image-analysis)
- Checked: 2026-10-12. Basis: user-supplied 19-page browser-exported PDF,
  `Guide for authors - Medical Image Analysis.pdf`; direct web retrieval returned 403.
- Source SHA-256: `8693c8129b4297f09b00501695282d0b2f320e35700078614dba2850948775b1`.
- Scope: Research Article. Publisher PDF is not redistributed or required at a
  machine-specific runtime path. Use with [MIRA's shared delivery contract](common.md).

## Official requirements (user-supplied Guide for Authors PDF snapshot)

Checked against the 19-page PDF exported on 2026-10-12 from the official
ScienceDirect guide. Page numbers below refer to that PDF, not manuscript pages.
This verifies the supplied snapshot, not future policy changes or submission
readiness. Do not require the author to supply it again merely to start drafting.

### Manuscript and supporting files (pp. 2, 7–15)

- Review is single anonymized; do not impose double-anonymous author removal.
  Include a title page with author names, affiliations and postal addresses,
  and the designated corresponding author's email. Request missing author details.
- Supply editable Word (.doc/.docx) or LaTeX (.tex) sources; a PDF alone is not an
  acceptable manuscript source. Word must be single-column; two-column layout is
  allowed only for LaTeX. The publisher encourages its LaTeX template, not mandatory
  LaTeX use. Preserve the requested DOCX working deliverable.
- Abstract: at most 250 words, self-contained, covering purpose, procedures,
  findings (statistical significance where applicable), conclusions and novelty.
  Avoid citations and uncommon abbreviations; define any essential abbreviations.
  The snapshot does not prescribe named abstract headings.
- Provide 1–7 English keywords.
- Highlights are required: a separate editable file whose filename includes
  "highlights", containing 3–5 bullets, each at most 85 characters including spaces.
- A graphical abstract is required as a separate file, at least 1328 pixels wide,
  with its longer side horizontal and text readable at 13 cm width. Preferred
  formats: TIFF, EPS, PDF or MS Office. A textual storyboard alone is not a completed
  graphical abstract. Track it as missing if no compliant visual can be produced.
- Number sections/subsections (1, 1.1, 1.1.1); leave the abstract unnumbered.
  A Discussion section is required and must interpret results against the cited
  literature. Use lettered appendices and appendix-specific equation/figure numbering.
- Use author–year citations, not IEEE-style numeric citations. Use both surnames
  for two authors; first surname plus et al. for three or more. Alphabetize the
  reference list, then order chronologically as needed; distinguish same-author,
  same-year entries with a/b suffixes. Verify every source and include available DOIs.
  Cite datasets and software with repository, version and persistent identifiers;
  label preprints and prefer the published version when available.
- Tables and equations must remain editable, not screenshots. Use SI units (or
  supply SI equivalents), numbered equations, table captions and in-text callouts.
  Avoid vertical table rules and cell shading.
- Supply figures separately with sequential names, callouts and captions. Vector
  artwork: EPS/PDF with fonts embedded or outlined. Raster photographs: at least
  300 dpi; bitmapped line drawings: at least 1000 dpi. Verify final-size legibility;
  do not claim compliant resolution from an uninspected image.
- Provide an editable biography of at most 100 words for each author. Acknowledge
  assistance in a separate section directly before references, not on the title page.
- The supplied guide specifies no overall manuscript word/page, reference-count
  or figure-count ceiling. Do not invent one or borrow limits from another journal.

### Declarations, data and AI use (pp. 4–7, 10–13)

- Obtain truthful funding/sponsor-role, competing-interest and CRediT contribution
  information. The competing-interest declarations tool produces a separate Word
  file for submission; a draft paragraph is not proof that this step was completed.
- Research-data policy Option C requires repository deposit plus citation/linking,
  or an explanation if sharing is not possible. A data-availability statement is
  required. Respect privacy and existing sharing restrictions: do not upload data
  or promise public code/data release without authorization.
- Disclose generative-AI writing assistance before the reference list, identifying
  the tool, purpose, human review and responsibility. Do not invent an underlying
  model name if only the MIRA service is known. Ask the author to verify the details.
- The supplied policy permits AI-assisted explanatory diagrams and reproducible,
  data-derived visualizations. It prohibits fabricating or altering primary observed
  patient/experimental imagery for illustration; it does not ban AI methods that
  are themselves the documented research. Disclose generative image assistance in
  the relevant caption and general AI statement. Retain original image provenance
  and disclose nonlinear image adjustments. Do not substitute synthetic results
  for measured data.

### Statement of Significance — required separate file (pp. 16–18)

Prepare a separate Word or PDF response document covering all 18 questions. The
guide warns that omission or irrelevant/incomplete answers cause desk rejection
without resubmission opportunity. These are summarized checkpoints, not verbatim
submission-form text. For final submission use the official questions with their
answers; verify wording against the supplied/current guide and flag that check if
the guide is unavailable in the delivery Run. Do not invent confirmations to fill it.

1. Prior journal submissions and improvements (answer at most 300 words). The guide
   does not permit resubmission of papers previously rejected by MedIA. If applicable,
   surface the conflict via request_workflow_interaction; user approval cannot waive
   the journal rule. Do not declare the manuscript eligible.
2. Prior peer-reviewed/conference version, its attached paper, and extensions stated
   explicitly in the Introduction (answer at most 300 words).
3. Recommend 3–5 suitable Associate Editors with rationale, based in countries
   different from the authors; exclude Executive Committee and Senior Editors.
   Check author/editor conflicts, including past collaboration; the guide requests
   explanation if a past collaborator is nominated. Never invent names, affiliations
   or absence of conflicts. Request a current board list and author confirmation;
   fetching it requires the workflow's existing research authorization.
4. Methodological innovation in 1–2 sentences (at most 300 words).
5. Differences from authors' previous publications, citing at least one if any
   exists; truthfully state if none exists (answer at most 300 words).
6. Identify 3–5 closest published studies, their differences, title, DOI, first and
   last authors, journal and year (answer at most 300 words).
7. Whether the method improves on the state of the art, and the actual metrics,
   datasets, baselines and statistical tests supporting any claimed gain.
8. Whether inputs retain full resolution and field of view; scientifically justify
   patches, downsampling, 2D slices or other restrictions when used.
9. Confirm figures cover the input's spatial extent; findings from 3D MRI must
   include at least two orthogonal planes. Missing views remain a gap, not a claim.
10. Confirm novelty information from questions 4–7 appears in the manuscript.
11. Reproducibility: multiple datasets including at least one public dataset for ML,
    stratified cross-validation or external testing, and leakage controls (including
    subject separation across folds). Explain absent evaluations; do not fabricate
    them or silently run new experiments.
12. For each dataset give public availability, size, inclusion/selection criteria,
    and training/testing counts unless cross-validation applies.
13. Dataset biases and how the analysis addresses them.
14. Public availability of both data and code, or justification for restrictions.
15. Confirm reproducibility information appears in the manuscript. The source
    explicitly refers to questions 9–12 here; preserve that reference when preparing
    the official form, while also checking that answers 13–14 are covered.
16. LLM-assisted writing: actual tools, uses, scientific-integrity/citation checks,
    and the corresponding manuscript disclosure.
17. Confirm absence of patient-identifying information only after author review
    and inspection of all supplied submission materials.
18. Additional comments to the publication office; ask whether any are needed.

Use request_workflow_interaction for missing submission history, author facts,
editor nominations/conflicts or claims that cannot be confirmed from the evidence.
Bundle related questions; retain TODOs in an approved working draft until answered.
For Challenge Reports only, a separate Bias Report is also required (p. 18); clarify
that article type instead of imposing it on every Research Article.
The manuscript, highlights, graphical abstract, significance statement, biographies,
declarations and compliance note belong to ONE delivery candidate, not separate
experimental results or independently selectable delivery candidates.

## Editorial recommendations (MIRA; not publisher requirements)

Frame the contribution as a medical-image-analysis method rather than a local
clinical application alone. Motivate the algorithmic gap and define the learning or
optimization problem. Describe representations, objectives and implementation in
reproducible detail. Use Introduction, Related Work where helpful, Methods,
Experiments/Results, Discussion and Conclusion as a provisional outline, not a
publisher-prescribed structure. Distinguish conceptual advances from parameter
tuning. Present fair baselines, ablations, dataset-specific results, robustness,
computational cost and failure analysis when available. Explain generalization
across anatomy, modality, sites or tasks only to the extent evaluated. Preserve
limitations of datasets and annotations; never invent a missing benchmark result.

## Preflight and unresolved checks (MIRA)

Reopen the generated files and measure the stated word, character and image limits.
Report manuscript and supporting-file readiness separately in a single manifest.
The checkpoints above summarize the supplied guide; the official significance
questions and any updated submission forms still need final wording verification.
Author confirmation is required for historical, personal and declaration facts.
An unresolved check is a visible TODO, not automatic certification or a reason to
invent evidence. Do not substitute an outline for a completed graphical file.
