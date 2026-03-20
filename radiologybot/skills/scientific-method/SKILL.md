---
name: scientific-method
description: "Scientific method workflow for research projects. Use this skill whenever starting a new experiment, analyzing results, planning next steps, or when the user asks to investigate a phenomenon. Enforces the observation→question→hypothesis→prediction→experiment→analysis→iterate cycle. Triggers on: 'experiment', 'investigate', 'why does', 'hypothesis', 'next experiment', 'analyze results', 'what should we try', 'research plan'."
---

# Scientific Method for Computational Research

## Overview

This skill enforces rigorous scientific methodology in computational research. It prevents the common trap of "method shopping" — trying techniques without understanding why — and ensures every experiment contributes to cumulative scientific understanding.

## The Scientific Cycle

```
┌─────────────┐
│ 1. OBSERVE  │ ← Examine data, results, literature
└──────┬──────┘
       ▼
┌─────────────┐
│ 2. QUESTION │ ← What specific phenomenon needs explaining?
└──────┬──────┘
       ▼
┌─────────────┐
│ 3. HYPOTHESIZE │ ← Propose a falsifiable mechanism
└──────┬──────┘
       ▼
┌─────────────┐
│ 4. PREDICT  │ ← Derive specific, testable expectations
└──────┬──────┘
       ▼
┌─────────────┐
│ 5. TEST     │ ← Design controlled experiment, execute
└──────┬──────┘
       ▼
┌─────────────┐
│ 6. ANALYZE  │ ← Compare results to predictions
└──────┬──────┘
       ▼
┌─────────────┐
│ 7. ITERATE  │ ← Revise hypothesis, ask new questions
└──────┴──────┘
       ↑ loops back to 1
```

## Detailed Guidance for Each Step

### Step 1: Observation

**Goal**: Establish facts before theorizing.

**Actions**:
- Examine raw data distributions, edge cases, failure modes
- Look at existing experimental results — not just aggregate metrics, but per-sample behavior
- Review relevant literature for known phenomena
- Visualize everything: histograms, scatter plots, example fits, residuals

**Output**: A factual summary of what is observed, with specific numbers.

**Template**:
```
## Observations
- [Metric X] shows mean=A ± B across N samples
- Visual inspection reveals [specific pattern]
- [N]% of samples show [specific failure mode]
- Literature reports [relevant finding] in similar settings
```

**Common mistakes**:
- ❌ Jumping to "the model is bad" without examining WHERE and HOW it fails
- ❌ Only looking at aggregate metrics, missing per-sample patterns
- ❌ Ignoring anomalies or outliers

---

### Step 2: Question

**Goal**: Identify the specific scientific question worth investigating.

**Criteria for a good question**:
- **Specific**: "Why does width estimation fail for PCr but not Pi?" (not "why is accuracy low?")
- **Answerable**: Can be addressed with available data and tools
- **Significant**: The answer would meaningfully advance understanding
- **Distinguishes science from engineering**: "Why does X happen?" vs "How do I make Y better?"

**Template**:
```
## Question
Given that [observation], why does [specific phenomenon] occur?
Specifically: [precise formulation]
```

**Hierarchy of question quality**:
1. 🥇 Mechanistic: "What causes X?" — leads to understanding
2. 🥈 Comparative: "Why does A work but B doesn't?" — reveals important factors
3. 🥉 Quantitative: "How does X scale with Y?" — maps the landscape
4. 🟡 Engineering: "What hyperparameter gives best results?" — useful but not science

---

### Step 3: Hypothesis

**Goal**: Propose a specific, falsifiable explanation.

**Criteria for a good hypothesis**:
- **Mechanistic**: Explains WHY, not just WHAT
- **Falsifiable**: There exists an observation that would prove it wrong
- **Specific**: Makes a concrete claim, not a vague direction
- **Parsimonious**: Doesn't invoke unnecessary complexity

**Template**:
```
## Hypothesis
[Phenomenon] occurs because [proposed mechanism].
This is because [reasoning/evidence supporting the mechanism].
This hypothesis would be falsified if [specific observation].
```

**Examples**:
- ✅ "Phase estimation fails in low-SNR spectra because the MSE loss landscape has multiple local minima separated by π, and gradient descent gets trapped in the wrong basin"
- ❌ "We need a better model" (not a hypothesis)
- ❌ "Deep learning should work better" (not falsifiable, not mechanistic)

---

### Step 4: Prediction

**Goal**: Derive specific, quantitative expectations from the hypothesis.

**Requirements**:
- State what you expect to observe IF the hypothesis is correct
- State what you would observe IF the hypothesis is wrong
- Be as quantitative as possible (directions, magnitudes, patterns)

**Template**:
```
## Predictions
If the hypothesis is correct:
- We should observe [specific outcome A] with [expected magnitude]
- [Metric X] should [increase/decrease] by approximately [amount]
- The effect should be [stronger/weaker] for [specific subset]

If the hypothesis is wrong:
- We would instead see [alternative outcome B]
- [Metric X] would remain [unchanged / change in opposite direction]
```

**Why this matters**: Without predictions, you can't distinguish between "the experiment confirmed my hypothesis" and "I'm just rationalizing whatever result I got."

---

### Step 5: Experiment Design & Execution

**Goal**: Test the prediction with a controlled experiment.

**Design principles**:
- **Change one variable at a time** (unless interaction effects are the hypothesis)
- **Include controls**: What's the baseline? What's the comparison?
- **Pre-register evaluation criteria**: Decide what metrics matter BEFORE seeing results
- **Ensure reproducibility**: Fixed seeds, version-controlled code, documented parameters

**Checklist before running**:
```
## Experiment Plan: ExpNNN
- **Independent variable**: [what we're changing]
- **Dependent variables**: [what we're measuring]
- **Control condition**: [baseline for comparison]
- **Sample size**: [how many samples, why this number]
- **Evaluation criteria**: [specific metrics and thresholds]
- **Success criterion**: [what result would support the hypothesis]
- **Failure criterion**: [what result would falsify the hypothesis]
```

**Git requirements**:
1. `git add` + `git commit` the experiment script BEFORE running
2. Commit message: `ExpNNN: <what and why>`
3. Record commit hash in experiment log

---

### Step 6: Analysis

**Goal**: Rigorously compare results to predictions.

**Requirements**:
- Report ALL pre-registered metrics (not just the ones that look good)
- Compare quantitatively to predictions from Step 4
- Include visual/qualitative assessment
- Look for unexpected patterns — they often contain the most information
- Perform sanity checks (do the numbers make physical sense?)

**Template**:
```
## Results
### Quantitative
| Metric | Predicted | Observed | Match? |
|--------|-----------|----------|--------|
| ...    | ...       | ...      | ✅/❌   |

### Qualitative
- Visual inspection shows [description]
- [N] failure cases examined: [common pattern]

### Unexpected findings
- [Anything not predicted that was observed]

## Interpretation
- The hypothesis is [supported / partially supported / falsified] because [evidence]
- The discrepancy in [metric] suggests [interpretation]
- Confidence level: [high/medium/low] because [reasoning]
```

---

### Step 7: Iterate

**Goal**: Update understanding and identify the next question.

**Actions**:
- If hypothesis supported → What's the next deeper question? Can we push further?
- If hypothesis falsified → What does the failure tell us? Revise the hypothesis.
- If results ambiguous → What additional experiment would disambiguate?
- Update MEMORY.md with conclusions
- Identify the single most important next question

**Template**:
```
## Conclusions & Next Steps
- **Learned**: [key insight from this experiment]
- **Updated understanding**: [how our mental model changed]
- **Next question**: [the most important thing to investigate next]
- **Proposed next experiment**: [brief sketch, to be fully designed in next cycle]
```

---

## Special Scenarios

### When the User Asks "What Should We Try Next?"

Do NOT immediately suggest a method. Instead:
1. Review recent experimental results (Observation)
2. Identify the biggest remaining gap in understanding (Question)
3. Propose a hypothesis about that gap
4. Only THEN suggest an experiment to test it

### When the User Suggests a Specific Method

It's fine to use user-suggested methods, but still:
1. Articulate WHY this method might work (implicit hypothesis)
2. State what we expect to see (prediction)
3. Define success/failure criteria before running

### When Results Are Unexpected

This is the most scientifically valuable situation:
1. Do NOT dismiss unexpected results as "noise" or "bugs"
2. First verify: Is the code correct? Is the data correct?
3. If verified: This is a new observation — start a new cycle from Step 1
4. Unexpected results often lead to the most important discoveries

### When Doing Exploratory Analysis

Full cycle not required, but:
1. Still document what you observe
2. Still formulate questions from observations
3. Save hypotheses for later testing — don't test them in the same exploratory session (avoid p-hacking equivalent)

---

## Research Project Structure

### Project-Level Organization

Each research project should maintain:

```
project/
├── README.md              # Project overview, scientific question, current status
├── EXPERIMENTS.md          # Log of all experiments (or in MEMORY.md)
├── data/                   # Raw and processed data
├── scripts/                # Experiment scripts (expNNN_description.py)
├── results/                # Output figures, metrics, logs
│   └── expNNN/
├── .gitignore
└── requirements.txt
```

### Experiment Naming Convention

- `expNNN_brief_description.py` — e.g., `exp014_phase_grid_search.py`
- Sequential numbering, never reuse numbers
- Description should reflect the QUESTION, not just the method

### Progress Tracking

Maintain a running summary in MEMORY.md:
- Current scientific understanding (what we know so far)
- Open questions (ranked by importance)
- Experiment history (with cross-references to git commits)
- Dead ends (what we tried and why it didn't work — equally valuable)
