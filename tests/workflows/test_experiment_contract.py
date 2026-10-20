"""Run-local acceptance must not depend on legacy project/global preferences."""

import pytest

from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.modules import ModuleExecutionContext, ModuleExecutorRegistry
from mira_engine.modules.tools import WorkflowExperimentTool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService
from mira_engine.workflows.experiments import experiment_contract


def start(tmp_path, version=2, profile="research", module="experiment"):
    service = WorkflowService(JsonWorkflowRepository(tmp_path, "contract-test"))
    data = {"experiment_contract_version": version} if module == "experiment" else {}
    run_id = service.create_run(command_id="create", module_type=module, module_data=data, enqueue=True).run_id
    service.claim_run(run_id, lease_owner="test", agent_profile=profile)
    return service, run_id


def save(service, run_id, record, command_id="record"):
    return service.command_run(run_id, command_id=command_id, command_type="record_experiment",
                               payload={"experiment": record})


def complete_record(tmp_path, profile):
    (tmp_path / "evidence.txt").write_text("Measured metric: 0.9")
    common = {"id": "Exp001", "title": "Controlled comparison", "status": "completed",
              "results": {"metrics": {"score": 0.9}, "artifacts": ["evidence.txt"]},
              "conclusion": "Evidence supports the tested hypothesis"}
    if profile == "engineer":
        return {**common, "commit": "abc123", "repro": {"script_path": "test.py", "seed": 0, "env": "python3"},
                "tests": {"summary": "Test passed"}}
    return {**common, "theoretical_proof": "Predicted effect under the stated assumptions",
            "isolation_test": {"control": "baseline", "treatment": "variant", "isolated_variable": "method"},
            "post_mortem": {"residual_analysis": "Residual quantified", "implementation_fidelity": "Implementation checked",
                            "five_whys": ["Observed change follows the isolated variable"]},
            "evidence_refs": [{"artifact": "evidence.txt", "metric_key": "score"}]}


@pytest.mark.parametrize("profile", ["research", "engineer", "team"])
@pytest.mark.parametrize("version", [1, 2])
def test_completion_acceptance_and_persistence(tmp_path, profile, version):
    service, run_id = start(tmp_path, version, profile)
    minimal = {"id": "Exp001", "title": "Baseline", "status": "completed"}
    if version == 2:
        with pytest.raises(WorkflowError, match="missing required fields"):
            save(service, run_id, minimal)
        assert "experiments" not in service.repository.load_run(run_id).module_data
        save(service, run_id, complete_record(tmp_path, profile))
    else:
        save(service, run_id, minimal)
    # Simulated restart: the selection and executing profile survive serialization.
    service = WorkflowService(JsonWorkflowRepository(tmp_path, "contract-test"))
    run = service.repository.load_run(run_id)
    assert experiment_contract(run, "engineer" if profile != "engineer" else "research")["profile"] == profile
    assert run.module_data["experiment_contract_version"] == version
    with pytest.raises(WorkflowError):
        service.command_run(run_id, command_id="change-mode", command_type="update_draft",
                            payload={"module_data": {"experiment_contract_version": 1}})
    service.command_run(run_id, command_id="complete", command_type="complete")
    assert service.repository.load_run(run_id).status == "completed"


def test_strict_complete_cannot_bypass_record_validation(tmp_path):
    service, run_id = start(tmp_path)
    with pytest.raises(WorkflowError, match="Register experiment outcomes"):
        service.command_run(run_id, command_id="empty", command_type="complete")
    run = service.repository.load_run(run_id)
    run.module_data["experiments"] = [{"id": "Exp001", "title": "Imported", "status": "completed"}]
    service.repository.save_run(run)
    with pytest.raises(WorkflowError, match="missing required fields"):
        service.command_run(run_id, command_id="bypass", command_type="complete")
    assert service.repository.load_run(run_id).status == "running"


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_strict_preserves_honest_unsuccessful_outcomes(tmp_path, status):
    service, run_id = start(tmp_path)
    save(service, run_id, {"id": "Exp001", "title": "Unavailable data", "status": status,
                          "conclusion": "Data unavailable; no successful experiment claimed"})
    service.command_run(run_id, command_id="complete", command_type="complete")


@pytest.mark.parametrize("version", [True, 0, 3, "2", None])
def test_invalid_mode_rejected_on_create_and_draft(tmp_path, version):
    service = WorkflowService(JsonWorkflowRepository(tmp_path, "contract-test"))
    with pytest.raises(WorkflowError, match="must be 1"):
        service.create_run(command_id="invalid", module_type="experiment", module_data={"experiment_contract_version": version})
    run_id = service.create_run(command_id="create", module_type="experiment").run_id
    with pytest.raises(WorkflowError, match="must be 1"):
        service.command_run(run_id, command_id="invalid-draft", command_type="update_draft",
                            payload={"module_data": {"experiment_contract_version": version}})
    service.command_run(run_id, command_id="strict-draft", command_type="update_draft",
                        payload={"module_data": {"experiment_contract_version": 2}})
    assert service.repository.load_run(run_id).module_data["experiment_contract_version"] == 2


@pytest.mark.parametrize("module", ["initiation", "planning", "delivery"])
def test_other_stages_unaffected(tmp_path, module):
    service, run_id = start(tmp_path, module=module)
    service.command_run(run_id, command_id="complete", command_type="complete")
    with pytest.raises(WorkflowError, match="only for experiment"):
        service.create_run(command_id="bad-module", module_type=module, module_data={"experiment_contract_version": 2})


def test_evidence_rechecked_at_completion_and_placeholders_rejected(tmp_path):
    service, run_id = start(tmp_path)
    record = complete_record(tmp_path, "research")
    save(service, run_id, record)
    (tmp_path / "evidence.txt").unlink()
    with pytest.raises(WorkflowError, match="does not exist"):
        service.command_run(run_id, command_id="complete", command_type="complete")
    record = complete_record(tmp_path, "research")
    record["theoretical_proof"] = "Guardrail auto-fill: no proof"
    with pytest.raises(WorkflowError, match="theoretical_proof"):
        save(service, run_id, record, "placeholder")


@pytest.mark.parametrize("module", ["initiation", "planning", "experiment", "delivery"])
def test_instructions_scope_and_legacy_preference_isolation(tmp_path, module):
    service, run_id = start(tmp_path, module=module)
    run = service.repository.load_run(run_id)
    context = ModuleExecutionContext(run=run, artifacts=(), project_dir=tmp_path,
                                     user_content="Test", metadata={"contract_version": 1}, agent_profile="research")
    prompt = ModuleExecutorRegistry().get(module).system_instructions(context)
    assert ("Experiment acceptance: STRICT" in prompt) == (module == "experiment")
    if module == "experiment":
        assert "theoretical_proof" in prompt
        assert "Never invent evidence" in prompt
        run.module_data.pop("experiment_contract_version")  # Older native Runs stay compatible.
        context.metadata["contract_version"] = 2
        assert "Experiment acceptance: COMPAT" in ModuleExecutorRegistry().get(module).system_instructions(context)


@pytest.mark.parametrize("profile", ["research", "engineer", "team"])
async def test_tool_error_is_recoverable_and_schema_accepts_profile_fields(tmp_path, profile):
    service, run_id = start(tmp_path, profile=profile)
    tool = WorkflowExperimentTool()
    tool.set_project_context("contract-test", str(tmp_path))
    tool.set_workflow_run_context(run_id)
    tools = ToolRegistry()
    tools.register(tool)
    error = await tools.execute(tool.name, {"experiment": {"id": "Exp001", "title": "Baseline", "status": "completed"}})
    assert "missing required fields" in error
    assert service.repository.load_run(run_id).status == "running"
    result = await tools.execute(tool.name, {"experiment": complete_record(tmp_path, profile)})
    assert '"status": "recorded"' in result
