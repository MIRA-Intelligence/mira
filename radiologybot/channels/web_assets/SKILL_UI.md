# Task Plan — SciAgentUI Extended Schema

This extends the base `task_plan` skill with UI-specific fields.
Write the file to your **Project Directory** (from Runtime Context — an absolute path):

```
write_file("{Project Directory}/task_plan.json", ...)
```

Always write the **full** JSON (not a patch).

## Pipeline Stages

| Stage | Purpose | Agent activities |
|-------|---------|-----------------|
| `research` | Background investigation | Literature search, gap analysis, hypothesis formation |
| `planning` | Experimental design | Create detailed execution plan with steps |
| `experiment` | Execution | Run experiments, collect data, record metrics |
| `writing` | Output | Analysis write-up, paper/report drafting |

## Schema — EXACT format required

```json
{
  "title": "Project title",
  "pipeline_stage": "research",
  "status": "in_progress",
  "started_at": "2026-03-24T12:00:00Z",
  "steps": [
    {
      "number": 1,
      "title": "Literature search on chest X-ray classification",
      "status": "completed",
      "stage": "research",
      "results": {
        "findings": "Found 3 key papers covering CNN, ViT, and hybrid approaches.",
        "artifacts": ["research/search_results.json"]
      }
    },
    {
      "number": 2,
      "title": "Design baseline experiments",
      "status": "running",
      "stage": "planning"
    },
    {
      "number": 3,
      "title": "Run ResNet-50 baseline",
      "status": "pending",
      "stage": "experiment"
    }
  ],
  "stage_data": {
    "research": {
      "papers": [
        {
          "id": "p1",
          "title": "CheXNet: Radiologist-Level Pneumonia Detection",
          "authors": "Rajpurkar et al.",
          "year": 2017,
          "venue": "arXiv",
          "doi": "10.48550/arXiv.1711.05225",
          "abstract": "We develop an algorithm that can detect pneumonia...",
          "relevance": "Established DenseNet-121 as baseline for CXR classification",
          "tags": ["baseline", "DenseNet", "CheXpert"]
        }
      ],
      "key_findings": [
        "DenseNet-121 achieves radiologist-level AUC on 14 pathologies",
        "Vision Transformers show 2-3% improvement over CNNs on CheXpert"
      ],
      "gaps": [
        "Few studies compare performance on multi-label vs binary classification"
      ],
      "summary": "Transfer learning with ImageNet-pretrained CNNs is the dominant approach..."
    },
    "writing": {
      "outline": [
        { "id": "s1", "title": "Introduction", "status": "completed", "word_count": 450 },
        { "id": "s2", "title": "Related Work", "status": "running", "word_count": 200 },
        { "id": "s3", "title": "Methods", "status": "pending" }
      ],
      "total_words": 650,
      "target_words": 5000
    }
  }
}
```

## Required top-level fields

| Field | Type | Values | Required |
|-------|------|--------|----------|
| `title` | `string` | — | YES |
| `pipeline_stage` | `string` | `research` · `planning` · `experiment` · `writing` | YES |
| `status` | `string` | `in_progress` · `completed` · `failed` | YES |
| `started_at` | `string` | ISO 8601 datetime | YES |
| `steps` | `array` | Array of step objects | YES |
| `stage_data` | `object` | Stage-specific data | NO |

## Step fields

| Field | Type | Required |
|-------|------|----------|
| `number` | `integer` | YES |
| `title` | `string` | YES |
| `status` | `string` (`pending`/`running`/`completed`/`failed`) | YES |
| `stage` | `string` (which pipeline stage this step belongs to) | YES |
| `phases` | `array` of `{label, status}` | NO |
| `results` | `object` with `metrics`, `findings`, `artifacts` | NO |

## stage_data.research

Populate this when you find papers or form conclusions during the research stage.

| Field | Type | Purpose |
|-------|------|---------|
| `papers` | `array` | Papers found — each with `id`, `title`, `authors`, `year`, `venue`, `doi`, `abstract`, `relevance`, `tags` |
| `key_findings` | `string[]` | Bullet-point findings from the literature |
| `gaps` | `string[]` | Identified research gaps |
| `summary` | `string` | Overall summary of the literature review |

## stage_data.writing

Populate this when you begin drafting the output document.

| Field | Type | Purpose |
|-------|------|---------|
| `outline` | `array` | Document sections — each with `id`, `title`, `status`, `word_count`, `preview` |
| `total_words` | `integer` | Current total word count |
| `target_words` | `integer` | Target word count |

## Rules

- Each step MUST have a `stage` field to associate it with a pipeline stage
- Only **one step** should be `running` at a time
- Advance `pipeline_stage` when transitioning between research/planning/experiment/writing
- Add papers to `stage_data.research.papers` as you find them during research
- Add `results` to a step when it completes — include key metrics and findings
- Update `stage_data.writing` as you draft the output document
