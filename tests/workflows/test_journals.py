from pathlib import Path

import pytest

from mira_engine.modules import DeliveryExecutor
from mira_engine.modules.base import ModuleExecutionContext
from mira_engine.workflows.journals import (
    PROFILE_VERSION,
    SOURCE_CHECK_DATE,
    journal_catalog,
    journal_instructions,
    normalize_journal_selection,
)
from mira_engine.workflows.repository import JsonWorkflowRepository
from mira_engine.workflows.service import WorkflowError, WorkflowService


@pytest.mark.parametrize("journal", journal_catalog(), ids=lambda item: item["id"])
def test_standalone_journal_files_include_sources_and_verification(journal):
    root = Path(__file__).resolve().parents[2] / "mira_engine" / "templates" / "journals"
    text = (root / f"{journal['id']}.md").read_text(encoding="utf-8")
    # Inspect the file itself: the runtime header used to mask missing links here.
    assert f"]({journal['guidelines_url']})" in text
    assert "## Sources and verification" in text
    assert f"Checked: {SOURCE_CHECK_DATE}" in text
    assert "## Official requirements" in text
    assert "## Editorial recommendations (MIRA" in text
    assert "## Preflight and unresolved checks (MIRA)" in text
    assert "[MIRA's shared delivery contract](common.md)" in text
    assert "source linked above" not in text
    assert "request_workflow_interaction" in text


@pytest.mark.parametrize("journal_id,checks", [
    ("radiology", ("≤3,000", "≤300", "Table 1", "Figure 1", "Double-anonymized", "separate", "DOCX")),
    ("radiology_ai", ("≤250", "≤255 characters", "CLAIM", "five keywords", "January 2026", "PDF-only")),
    ("mrm", ("≤5,000", "Single-blind", "SHA-1", "Code Review", "Equal-contribution", "checklist says two")),
    ("jmri", ("≤4,000", "Field Strength/Sequence", "Stage of Technical Efficacy", "LIMITATIONS", "parentheses", "LaTeX")),
    ("ieee_tmi", ("≤10 pages", "<40 MB", "at least 10-point", "prose supplement", "Previous rejection", "Optional")),
    ("european_radiology", ("mandatory\nat revision", "single-blinded", "1–5", "3–5", "Disclosure Paragraph", "resubmission invitation")),
])
def test_journal_specific_rules_and_source_conflicts_reach_prompt(journal_id, checks):
    prompt = journal_instructions({"delivery_format": "paper_article", "target_journal": journal_id})
    for check in checks:
        assert check in prompt
    assert "User approval cannot waive a publisher rule" in prompt


def test_catalog_has_seven_distinct_prompts_and_official_sources():
    catalog = journal_catalog()
    assert len(catalog) == 7
    assert len({item["id"] for item in catalog}) == 7
    prompts = []
    for item in catalog:
        prompt = journal_instructions({"delivery_format": "paper_article", "target_journal": item["id"]})
        assert item["guidelines_url"] in prompt
        assert item["name"] in prompt
        assert "Editorial recommendations" in prompt
        assert "Do not invent patients" in prompt
        prompts.append(prompt)
    assert len(set(prompts)) == 7
    assert journal_instructions({"delivery_format": "paper_article"}) == ""
    media = next(item for item in catalog if item["id"] == "media")
    assert media["verification_status"] == "verified"
    assert media["checked_at"] == SOURCE_CHECK_DATE
    assert media["profile_version"] == PROFILE_VERSION


def test_media_pdf_requirements_and_evidence_boundaries_reach_prompt():
    prompt = journal_instructions({"delivery_format": "paper_article", "target_journal": "media"})
    for requirement in (
        "19-page PDF", "single anonymized", "at most 250 words", "1–7 English keywords",
        "3–5 bullets", "85 characters including spaces", "at least 1328 pixels",
        "Discussion section is required", "author–year citations", "Option C",
        "CRediT", "at most 100 words", "all 18 questions", "3–5 suitable Associate Editors",
        "two orthogonal planes", "subject separation across folds", "questions 9–12",
        "request_workflow_interaction", "ONE delivery candidate", "Challenge Reports only",
    ):
        assert requirement in prompt
    assert "verification pending" not in prompt
    assert "NOT a\nverified formatting template" not in prompt
    assert "Do not invent confirmations" in prompt


@pytest.mark.parametrize("old_version", ["2026-10-12", "2026-10-12.2"])
def test_previous_profile_requires_reconfirmation(old_version):
    with pytest.raises(ValueError, match="reload the catalog"):
        normalize_journal_selection("delivery", {
            "delivery_format": "paper_article", "target_journal": "media",
            "journal_profile_version": old_version,
        })


@pytest.mark.parametrize("module,data", [
    ("delivery", {"delivery_format": "paper_article", "target_journal": "../../secret"}),
    ("delivery", {"delivery_format": "paper_article", "target_journal": []}),
    ("delivery", {"delivery_format": "presentation", "target_journal": "mrm"}),
    ("experiment", {"delivery_format": "paper_article", "target_journal": "mrm"}),
    ("delivery", {"delivery_format": "paper_article", "target_journal": "mrm", "journal_profile_version": "old"}),
    ("delivery", {"journal_profile_version": PROFILE_VERSION}),
])
def test_invalid_selections_rejected(module, data):
    with pytest.raises(ValueError):
        normalize_journal_selection(module, data)


@pytest.mark.parametrize("journal_id,expected", [
    ("jmri", ("Field Strength/Sequence", "Stage of", "Journal of Magnetic Resonance Imaging")),
    ("media", ("Statement of Significance", "all 18 questions", "Verification status: verified")),
])
async def test_selection_survives_disk_reload_and_reaches_system_prompt(tmp_path: Path, journal_id, expected):
    repository = JsonWorkflowRepository(tmp_path, "PRJ-journal")
    service = WorkflowService(repository)
    receipt = service.create_run(command_id="create", module_type="delivery", module_data={
        "intent": "Write the paper", "delivery_format": "paper_article", "target_journal": journal_id,
    })
    reloaded = JsonWorkflowRepository(tmp_path, "PRJ-journal").load_run(receipt.run_id)
    assert reloaded.module_data["journal_profile_version"] == PROFILE_VERSION
    context = ModuleExecutionContext(run=reloaded, artifacts=(), project_dir=tmp_path, metadata={}, user_content="Write the paper", agent_profile="engineer")
    executor = DeliveryExecutor()
    assert (await executor.validate_inputs(context)).valid
    prompt = executor.system_instructions(context)
    for text in expected:
        assert text in prompt
    assert "Target journal: Radiology\n" not in prompt


def test_service_rejects_bad_selection_and_pins_draft_updates(tmp_path: Path):
    service = WorkflowService(JsonWorkflowRepository(tmp_path, "PRJ-journal"))
    with pytest.raises(WorkflowError) as exc:
        service.create_run(command_id="bad", module_type="delivery", module_data={"delivery_format": "paper_article", "target_journal": "unknown"})
    assert exc.value.status == 422
    receipt = service.create_run(command_id="create", module_type="delivery", module_data={"delivery_format": "paper_article"})
    service.command_run(receipt.run_id, command_id="update", command_type="update_draft", payload={"module_data": {"target_journal": "mrm"}})
    assert service.repository.load_run(receipt.run_id).module_data["journal_profile_version"] == PROFILE_VERSION
