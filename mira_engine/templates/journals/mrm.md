# Magnetic Resonance in Medicine — Research Article

## Sources and verification

- Official author guide: [MRM Author Guidelines](https://onlinelibrary.wiley.com/page/journal/15222594/homepage/author-guidelines)
- Official journal: [Magnetic Resonance in Medicine](https://onlinelibrary.wiley.com/journal/15222594)
- Checked: 2026-10-12. Basis: official guide body retrieved in this audit,
  including preparation, data availability and submission-checklist sections.
  Previously only indexed excerpts had been accessible.
- Scope: initial Research Article. This is a scoped specification with execution
  checks, not a complete reproduction of every publisher policy.
  Use with [MIRA's shared delivery contract](common.md).

## Official requirements

### Length and outline — guide sections on manuscript preparation

- Body including appendices ≤5,000 words; figures plus tables ≤10; citations uncapped.
- Abstract ≤250 words: Purpose, Methods, Results, Conclusion; optional Theory and
  Methods. Passive voice; avoid first person. Keywords: 3–6.
- Body: Introduction, optional Theory, Methods, Results, Discussion, Conclusions;
  final two sections may merge.
- Layout: 1.5 spacing; ≥11-point; numbered pages. Word preferred; LaTeX/RTF accepted.

### Files, review and citations — preparation/checklist sections

- Single-blind review; one corresponding author; identifying title page.
- Cover letter, conflicts document, manuscript, figure/caption material and review PDF.
- Editable tables; numbered bracket citations; consistent initial reference style
  may be free-form. List all authors through six; otherwise first three plus et al.
- Human/animal approvals must be stated.

### Data and conditional requirements — data availability/authorship sections

- Sharing encouraged; software statement requests repository/DOI and revision SHA-1.
- Optional Code Review is requested at initial submission, not revision.
- Equal-contribution section allows three; checklist says two: unresolved discrepancy.
- Hardware papers generally need experimental demonstration; explain exceptions.

## MIRA execution and clarification (not additional publisher rules)

### Intake and evidence

Use request_workflow_interaction for an unsupported article type, conflicting target
or consequential missing information. Ask for the intended contribution, selected
experiment, available result files and submission stage as one focused group of
questions. Never treat this profile as authority to acquire new measurements.

Build a scientific evidence inventory: model equations, units, acquisition parameters,
reconstruction settings, calibration, reference measurements, uncertainty and failure
cases. Mark simulation, phantom and human evidence separately. An unavailable tier
is an explicit limitation, not a result waiting to be invented.

### Drafting and file production

Plan the argument around a precise MR problem, assumptions, proposed change and
available verification. Keep symbolic definitions consistent across manuscript,
figures and source code. Resolve ambiguous units or normalization before interpreting
a claimed gain. Preserve working equations as editable objects in the requested DOCX;
inspect their rendering after export.

Use a file manifest to distinguish the main draft, source figures, display tables,
review rendering and optional extras. Do not assume that creation of a DOCX also
created the review PDF. Reopen each generated file and check corrupt symbols,
clipping, cross-references and caption order. All files belong to ONE delivery
candidate; the manifest does not create additional selectable research results.

### Author-owned information

Collect the actual author order, correspondence choice, affiliations, contributions,
funding, conflicts, consent and permissions. Do not certify them on the author's
behalf. For multiple equal contributors, present the conflicting source sections
and request the author's editorial clarification rather than selecting the more
permissive statement. An author preference is not a publisher waiver.

For any shared implementation, distinguish code actually used from a convenient
repository's current default branch. Do not invent a commit, license, public access
or maintenance promise. Sharing restrictions remain subject to the project's privacy
and workspace permissions.

### Completion checks

Calculate counts on the exported document using the stated scope. Measure symbol,
equation, table and figure consistency separately from scientific validity. Produce
a compliance note with requirement, observed value/evidence, source section, status
and remaining owner. Retain an approved draft with TODOs if a factual answer is
missing; do not call it submission-ready.

## Editorial recommendations (MIRA)

Center the writing on MR physics, acquisition, reconstruction, quantification,
hardware or spectroscopy. Explain identifiability and uncertainty where relevant:
a low residual does not itself validate a physiological parameter. Compare the
actual baselines fairly, retaining acquisition and input-information boundaries.
Discuss mechanism, accuracy, precision, robustness and computational cost before
speculative clinical benefits. Distinguish a feasibility observation from broad
validation and a post hoc explanation from an originally tested hypothesis.

## Preflight and unresolved checks (MIRA)

The equal-contribution discrepancy remains open. Do not borrow JMRI's clinical
abstract, MedIA's author–year bibliography or another journal's mandatory graphical
abstract. This audit does not pin an MRM-specific AI-disclosure location or all
production artwork/template details: retain the shared truthful-disclosure rule
and verify current publisher instructions before final submission. A revision,
Toolbox Article or editor exception requires its own author-supplied instructions.
