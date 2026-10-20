"""Read-only product QA: temporary projects, scripted provider, no external calls."""
import json
import runpy
from pathlib import Path

import pytest

from mira_engine.modules.tools import WorkflowInteractionTool
from mira_engine.workflows.confirmations import confirmation_approved, confirmation_required
from mira_engine.workflows.models import InitiationResearchResult

helpers = runpy.run_path('/Users/cwang/Code/mira/tests/workflows/test_executors.py')
globals().update({name: helpers[name] for name in (
    '_service', '_ScriptedProvider', 'LLMResponse', 'ToolCallRequest',
    'ResearchAgentLoop', 'MessageBus', 'ChannelsConfig', 'ExecToolConfig',
    'SessionManager', 'ModuleExecutionContext', 'InitiationExecutor',
)})


@pytest.mark.asyncio
async def test_vague_goal_passes_input_validation(tmp_path):
    service = _service(tmp_path)
    receipt = service.create_run(command_id='create', module_type='initiation',
        module_data={'intent': '帮我调研肝脏MRI', 'confirmation_policy': {'mode': 'required'}})
    run = service.repository.load_run(receipt.run_id)
    context = ModuleExecutionContext(run=run, artifacts=(), project_dir=service.repository.project_dir,
        user_content='帮我调研肝脏MRI', metadata={}, agent_profile='research')
    validation = await InitiationExecutor().validate_inputs(context)
    assert validation.valid
    assert 'expected research outcome' in InitiationExecutor().system_instructions(context)


@pytest.mark.asyncio
async def test_initiation_search_is_blocked_until_form_requested(tmp_path, monkeypatch):
    monkeypatch.setattr('mira_engine.config.paths.get_workspace_path', lambda *args, **kwargs: tmp_path)
    service = _service(tmp_path)
    receipt = service.create_run(command_id='create', module_type='initiation', enqueue=True,
        module_data={'intent': '帮我调研肝脏MRI', 'confirmation_policy': {'mode': 'required'}})
    run_id = receipt.run_id
    service.claim_run(run_id, lease_owner='qa:scripted')
    provider = _ScriptedProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id='search', name='web_search',
            arguments={'query': 'liver MRI'})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id='ask', name='request_workflow_interaction',
            arguments={'interaction_type': 'approve_research_scope', 'title': '确认调研范围',
                'prompt': '研究肝脏MRI；请确认预期成果。',
                'choices': [{'id': 'approve', 'label': '批准'}, {'id': 'revise', 'label': '修改'}],
                'allow_freeform': True})]),
        LLMResponse(content='等待确认。'),
    ])
    loop = ResearchAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
        model='dummy/native', channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5), session_manager=SessionManager(tmp_path))
    await loop.process_direct('帮我调研肝脏MRI', session_key=f'ui:{service.repository.project_id}:run:{run_id}',
        channel='ui', chat_id=service.repository.project_id, metadata={
            'loop_mode': 'project', 'project_id': service.repository.project_id,
            'project_dir': str(service.repository.project_dir), 'workflow_run_id': run_id})
    assert '"blocked_tool":"web_search"' in json.dumps(provider.calls[1]['messages'], ensure_ascii=False).replace('\\"', '"')
    waiting = service.repository.load_run(run_id)
    assert waiting.status == 'waiting_input'
    assert waiting.module_data['interaction']['interaction_type'] == 'approve_research_scope'


@pytest.mark.asyncio
@pytest.mark.parametrize('selection,notes,approved', [
    ('revise', '请整理3个研究方向，每个包含创新点、数据需求和可行性。', False),
    ('approve', '请补充3个研究方向。', True),
    ('请整理3个研究方向', None, False),
])
async def test_brief_without_deliverable_is_accepted_and_response_semantics(tmp_path, selection, notes, approved):
    service = _service(tmp_path)
    receipt = service.create_run(command_id='create', module_type='initiation', enqueue=True,
        module_data={'intent': '帮我调研肝脏MRI', 'confirmation_policy': {'mode': 'required'}})
    run_id = receipt.run_id
    service.claim_run(run_id, lease_owner='qa:tool')
    tool = WorkflowInteractionTool()
    tool.set_project_context(service.repository.project_id, str(service.repository.project_dir))
    tool.set_workflow_run_context(run_id)
    result = json.loads(await tool.execute(interaction_type='approve_research_scope',
        title='调研确认', prompt='检索肝脏MRI相关资料。',
        choices=[{'id': 'approve', 'label': '批准'}, {'id': 'revise', 'label': '修改'}], allow_freeform=True))
    assert result['status'] == 'waiting_input'
    response = {'selection': selection}
    if notes is not None:
        response['notes'] = notes
    service.command_run(run_id, command_id='respond', command_type='submit_interaction',
        payload={'interaction_id': result['interaction_id'], 'response': response})
    run = service.repository.load_run(run_id)
    assert run.status == 'queued'
    assert run.module_data['interaction_history'][-1]['response'] == response
    assert confirmation_approved(run) is approved


def test_api_run_without_confirmation_policy_does_not_require_confirmation(tmp_path):
    service = _service(tmp_path)
    receipt = service.create_run(command_id='create', module_type='initiation',
        module_data={'intent': '帮我调研肝脏MRI'})
    assert not confirmation_required(service.repository.load_run(receipt.run_id))


def test_research_result_schema_allows_zero_directions():
    assert InitiationResearchResult(survey='调研总结').directions == []
