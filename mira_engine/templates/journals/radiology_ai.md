# Radiology: Artificial Intelligence — Original Research

## Sources and verification

- Official author guide: [Radiology: AI author instructions](https://pubs.rsna.org/page/ai/author-instructions)
- Preparation checklist: [Radiology: AI manuscript checklist](https://pubs.rsna.org/page/ai/author-instructions/checklist)
- Ethics and disclosure: [RSNA editorial policies](https://pubs.rsna.org/page/policies)
- Checked: 2026-10-12. Basis: retrieved official web pages. Scope: initial
  Original Research; other article types and revisions need their own checks.
  This is an independently linked writing specification, not a verbatim guide.
  Use with [MIRA's shared delivery contract](common.md).

## Official requirements

### Manuscript contract — author guide

| Component | Requirement |
| --- | --- |
| Main text | ≤3,000 words |
| Body headings | Introduction; Materials and Methods; Results; Discussion |
| Abstract | ≤250 words; Purpose; Materials and Methods; Results; Conclusion |
| References / figures / tables | ≤35 / 6 / 4 |
| Key Points | ≤3, supported by numerical findings |
| Summary sentence | ≤255 characters |
| Review | Double-anonymized; identifying title page separate |
| Anonymized title page | Title, type, summary and Key Points |
| Text | Double-spaced, 11-point; no manually added page numbers |

The full title page carries authors, affiliations, correspondence, funding,
acknowledgments and data sharing. Explain overlapping cohorts and prior conference
or preprint versions, and provide the related papers. Use the applicable reporting
checklist; medical-imaging AI uses CLAIM, with its linked 2024 update.

The January 2026 notice requires DOCX, although older formatting text still mentions
PDF. Do not treat PDF-only delivery as confirmed compliant.

### Submission files and checks — preparation checklist

Send the cover letter and full title page separately. The manuscript contains the
anonymous front matter, abstract, body, references, captions and tables. Supply
initial figures together in a separate text document with adjacent captions;
revision artwork is separate. Keep supplementary prose/tables/figures together,
multimedia separate. The checklist asks for five keywords and at most 10 listed
abbreviations.

Results and Discussion have respective 1,000- and 800-word ceilings. Match results
to the methods, including negative findings; supply numerators/denominators for
percentages. Put limitations before the concluding paragraph, and do not cite
figures/tables in Discussion. Image minimums are 300 dpi, or 1200 dpi for line art,
with dimensions at least 3 × 3 inches. Captions identify imaging plane, contrast,
symbols and relevant subject information.

### Declarations — RSNA policies

AI assistance belongs in both manuscript and cover letter, with tool/version and
purpose; verify attribution and citations. Report actual ethics/consent and trial
registration. Describe data access and restrictions truthfully. Do not silently
promise public release or certify a manuscript as exclusively submitted.

## MIRA execution and clarification (not additional publisher rules)

1. Start with a task-and-evidence map: intended user, clinical input, model output,
   dataset role, selected experiment and approved conclusion. Mark what was measured
   separately from what deployment might eventually achieve.
2. Inspect the supplied split manifests and result artifacts. Identify the unit of
   independence, including patients/sites/times where applicable. If this cannot be
   established, ask through request_workflow_interaction; do not repair evidence
   gaps by inventing a locked test set or running an unauthorized experiment.
3. Draft the methods from the actual code/configuration, including preprocessing,
   annotation, training selection and evaluation settings where available.
   Missing implementation detail remains an explicit request to the user.
4. Fill the reporting checklist with manuscript locations and evidence pointers.
   A checked box means the item is genuinely reported, not that the agent believes
   a normal study would have done it.
5. Obtain author confirmation for submissions, prior versions, subject overlap,
   permissions and disclosure facts. Keep this separate from drafting mechanics.
   Scan all reviewer-visible material for identity leaks without deleting source
   documents. Record any anonymous repository-link problem for author resolution.
6. Keep all submission-support files under ONE delivery candidate. If DOCX export,
   artwork conversion or metadata inspection fails, state the failure explicitly.

## Editorial recommendations (MIRA)

Write for a radiologist evaluating whether an AI result is meaningful. Explain the
clinical comparator and intended setting before model detail. Use genuine external
validation, calibration, subgroup results, uncertainty or reader/workflow impact
when supplied; never imply these were assessed merely because they are desirable.
Discuss failure cases and labeling limitations. A statistically better score does
not establish clinical readiness. Translate architecture choices into testable
reasons, not promotional language.

## Preflight and unresolved checks (MIRA)

Measure text, section and character budgets independently. Test the exported
reviewer document and support files, including figure readability and cross-file
consistency. Record which CLAIM edition/form was actually used instead of silently
combining versions. The DOCX/PDF discrepancy remains documented: DOCX satisfies
the newer notice and the existing MIRA deliverable, while a PDF-only request needs
clarification. The checklist overview is not permission to omit Methods. Consult
the linked official guide for current portal-specific details; do not borrow MedIA
highlights, significance questions or graphical-abstract requirements.
