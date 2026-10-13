# Target-journal writing profiles

Checked: 2026-10-12. Scope: initial original-research manuscripts, not reviews,
letters, technical notes or revision instructions. Prompts are original summaries,
not copies of publisher guides. Clinical/engineering writing recommendations are
MIRA editorial advice and are labelled separately from verified requirements.

| Journal | Writing emphasis | Official author guide | Verification |
| --- | --- | --- | --- |
| Radiology | Clinical decision, diagnostic evidence and applicability | https://pubs.rsna.org/page/radiology/author-instructions | Official page snapshot |
| Radiology: Artificial Intelligence | Clinical AI evaluation and translation | https://pubs.rsna.org/page/ai/author-instructions | Official page snapshot |
| Magnetic Resonance in Medicine | MR physics, acquisition, reconstruction and methodological validation | https://onlinelibrary.wiley.com/page/journal/15222594/homepage/author-guidelines | Official guide body retrieved in revision 3 audit; previously only indexed text |
| Journal of Magnetic Resonance Imaging | MRI diagnostic applications and structured clinical reporting | https://onlinelibrary.wiley.com/page/journal/15222586/homepage/forauthors.html | Official page snapshot |
| Medical Image Analysis | Image-analysis method, benchmarking and generalization | https://www.sciencedirect.com/journal/medical-image-analysis/publish/guide-for-authors | Verified against user-supplied 19-page official-guide PDF exported 2026-10-12; direct web retrieval still blocked |
| IEEE Transactions on Medical Imaging | Engineering novelty, rigorous evaluation and reproducibility | https://ieeetmi.org/authors-instructions/ | Current official page; prefer this to the older EMBS mirror |
| European Radiology | Clinical relevance, patient-facing interpretation and generalizability | https://journals.myesr.org/eur-radiol/for-authors/article-types/ | Current official page; prefer it to older manuscript-requirements PDFs |

## Product behavior

- Both direct Delivery launch and downstream handoff expose the same journal selector
  only for Journal Article. Generic format remains available for undecided targets
  and older/offline engines. No target is guessed from the project topic.
- The engine owns the catalog and trusted prompts. The client persists only
  `target_journal` and `journal_profile_version` alongside `delivery_format`.
  Unknown IDs, mismatched profile versions and non-article uses are rejected.
- Journal choice survives Run persistence/resumption, without storing raw prompts
  supplied by the client. Generic article output remains backward compatible.
- Independent prompts: `mira_engine/templates/journals/<id>.md`; shared evidence and
  approval boundaries: `common.md`. Catalog/API: `workflows/journals.py` and
  `GET /api/workflow/v1/journals`.
- A profile targets a manuscript draft, not automatic submission. TMI PDF/layout
  checks and other supporting files are tracked separately. All supporting files
  remain part of one delivery candidate. Do not claim submission compliance from
  a DOCX existing on disk.
- MedIA is verified against the supplied PDF snapshot. Its selector no longer needs
  the unverified-guide warning when connected to the updated engine. Requirements
  cover the manuscript and supporting submission files, not just a writing style.
- Selecting a journal does not authorize new literature research, experiments,
  disclosure of project data, or bypass of approval. Source refresh requires the
  author's supplied guide or a separately authorized research Run.

## Maintenance

Current catalog revision: `2026-10-12.3`; source-check date: `2026-10-12`.
Revision 2 replaces the provisional MedIA preset. The revision and check date are
separate fields so a same-day correction still invalidates a stale selection.
Existing runs pinned to earlier revisions require reconfirmation under the existing
catalog-version policy (catalog-wide, not limited to MedIA).

Update each prompt against the current official guide, recording whether guidance
is a requirement, recommendation or unresolved item. Change `PROFILE_VERSION` when
requirements change. Existing runs with an unavailable revision must stop for an
explicit reconfirmation, not silently adopt a new format. Publisher links and
policies can change; all generated compliance notes require pre-submission review.

## Revision 3: standalone sources and detailed execution specifications

The first six profiles relied on the source URL injected by `journal_instructions()`
and used “source linked above” in the Markdown body. That worked in assembled model
context but left standalone files without an actual link. All seven files now have
their own clickable official-guide links, inspection date, scope and evidence basis.
Tests inspect the raw Markdown, not just the injected model prompt.

The six shorter profiles now separate manuscript constraints, file organization,
declarations, MIRA execution/clarification, editorial advice and final checks. They
are scoped, original specifications—not full reproductions of copyrighted guides
and not a claim to cover every linked production policy or submission form.
Each profile records its remaining checks. The shared prompt preserves that boundary.

Additional official pages checked for this audit:

- Radiology: [preparation checklist](https://pubs.rsna.org/page/radiology/author-instructions/checklist),
  [scientific style](https://pubs.rsna.org/page/radiology/author-instructions/scientificediting).
- Radiology: AI: [preparation checklist](https://pubs.rsna.org/page/ai/author-instructions/checklist).
- Both RSNA profiles: [editorial policies](https://pubs.rsna.org/page/policies).
- TMI: [submission checklist](https://ieeetmi.org/submission-checklist/),
  [author FAQ](https://ieeetmi.org/faqs-for-authors/).
- European Radiology: [mandatory files](https://journals.myesr.org/eur-radiol/for-authors/mandatory-files/),
  [artwork and revision requirements](https://journals.myesr.org/eur-radiol/for-authors/optional-files/),
  [general information](https://journals.myesr.org/eur-radiol/for-authors/general-information/).

Source-disagreement handling is now explicit in the relevant files. In particular,
the European Radiology graphical-abstract requirement is stage-sensitive; keywords
can use the overlapping published range. RSNA's newer DOCX notice takes precedence
for the working deliverable over older PDF wording. Authorship/anonymity ambiguities
are surfaced for author/editor clarification rather than silently resolved by MIRA.
Publisher requirements cannot be waived by user approval in MIRA.

## MedIA source audit

- Source: user-supplied `Guide for authors - Medical Image Analysis.pdf`, 19 pages,
  saved from ScienceDirect on 2026-10-12. Local original remains outside the repo at
  `/Users/cwang/Shared/Guide for authors - Medical Image Analysis.pdf`; no runtime
  dependency on this machine-specific path and no publisher PDF redistributed.
- SHA-256: `8693c8129b4297f09b00501695282d0b2f320e35700078614dba2850948775b1`.
- pp. 2, 7–10: single-anonymized review; editable single-column Word or LaTeX;
  abstract ≤250 words; 1–7 keywords; required separate highlights (3–5 bullets,
  ≤85 characters each including spaces); required landscape graphical abstract
  ≥1328 pixels wide, readable at 13 cm width; editable tables/equations and artwork.
- pp. 11–15: Option C data policy and availability statement; numbered sections;
  required Discussion; CRediT; author biographies ≤100 words; author–year citations.
- pp. 5, 7, 10–11: writing-AI disclosure and image-provenance rules. The snapshot
  permits AI-assisted explanatory graphics/reproducible visualizations, but not
  fabricated primary observations. This is snapshot-specific, not inferred from
  older Elsevier policies.
- pp. 16–18: required Statement of Significance, all 18 questions; includes prior
  submissions, novelty, editor nominations, full-field imaging, reproducibility,
  dataset counts/bias, data/code sharing, LLM disclosure and patient privacy.
  Questions 1, 2, 4, 5, 6 have 300-word ceilings; Q4 also asks for 1–2 sentences.
  Preserve the source's Q15 cross-reference to Q9–12 rather than silently correcting
  it. Missing facts trigger clarification, never invented answers. Final form
  requires original official question wording; the prompt contains paraphrased
  checkpoints, not a verbatim publisher template.
- Challenge Report Bias Reports are conditional, not required for all papers.
  No overall manuscript length/reference-count/figure-count limit was found in
  this snapshot. No such limit has been invented in the prompt.
- Verification means this supplied source was checked. Final submission still
  requires author review of the current guide, all declarations and actual files.
