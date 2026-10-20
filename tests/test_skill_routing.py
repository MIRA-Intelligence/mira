"""Routing requires current intent, not self-description or unrelated history."""

import json
from pathlib import Path

import pytest

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.agent.skills import SkillsLoader
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig
from mira_engine.providers.base import LLMProvider, LLMResponse


def make_loader(tmp_path):
    contents = {
        "sheet-helper": ('spreadsheet table workbook formula', ['spreadsheet', 'table', 'workbook', 'formula']),
        "scan-helper": ('tomography volumetric reconstruction', ['ct', 'mri']),
    }
    for name, (description, aliases) in contents.items():
        path = tmp_path / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: {description}\naliases: {json.dumps(aliases)}\n---\n{name} instructions\n')
    return SkillsLoader(tmp_path, builtin_skills_dir=None)


def test_self_description_is_not_query_evidence(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.suggest_skills('unrelatedquasar') == []
    assert loader.suggest_skills('spreadsheet') == ['sheet-helper']


@pytest.mark.parametrize('query', ['unrelatedquasar', 'previous weather report', '之前天气怎么样', '不要继续旧任务', 'Do not continue the last task', 'resume.pdf'])
def test_history_requires_affirmative_continuation(tmp_path, query):
    loader = make_loader(tmp_path)
    assert loader.suggest_skills(query, recent=['sheet-helper']) == []


@pytest.mark.parametrize('query', ['继续之前的任务', '请接着处理', 'Please continue', 'resume the previous task'])
def test_explicit_continuation_preserves_recent_skill(tmp_path, query):
    loader = make_loader(tmp_path)
    assert loader.suggest_skills(query, recent=['sheet-helper']) == ['sheet-helper']


def test_short_aliases_do_not_match_inside_unrelated_words(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.suggest_skills('introduction') == []  # "ct" is not a CT request.
    assert loader.suggest_skills('CT') == ['scan-helper']


def test_installation_path_is_not_routing_intent(tmp_path):
    loader = make_loader(tmp_path / 'unrelatedquasar')
    assert loader.suggest_skills('unrelatedquasar') == []


class CapturingProvider(LLMProvider):
    def __init__(self):
        self.messages = []

    def get_default_model(self):
        return 'dummy/routing'

    async def chat(self, **kwargs):
        self.messages.append(kwargs['messages'])
        return LLMResponse(content='Done')


@pytest.mark.parametrize('loop_class', [BaseAgentLoop, ResearchAgentLoop])
@pytest.mark.parametrize('query,manual,expected', [
    ('unrelatedquasar', False, []),
    ('继续之前的任务', False, ['sheet-helper']),
    ('unrelatedquasar', True, ['sheet-helper']),
    ('CT', False, ['scan-helper']),
])
async def test_actual_prompt_and_load_audit_do_not_reinsert_unrelated_history(tmp_path, monkeypatch, loop_class, query, manual, expected):
    monkeypatch.setenv('MIRA_HOME', str(tmp_path / 'home'))
    loader = make_loader(tmp_path)
    provider = CapturingProvider()
    loop = loop_class(bus=MessageBus(), provider=provider, workspace=tmp_path,
                      model='dummy/routing', channels_config=ChannelsConfig())
    loop.context.skills = loader
    loop.sessions.get_or_create('ui:chat').metadata['_recent_skills'] = ['sheet-helper']
    loaded = []

    async def audit(details):
        if details.get('loaded'):
            loaded.append(details['skill_name'])

    await loop._process_message(InboundMessage(channel='ui', sender_id='u', chat_id='chat', content=query,
        metadata={'run_mode': 'manual', **({'selected_skill_ids': ['sheet-helper']} if manual else {})}), audit_hook=audit)
    assert loaded == expected
    system = provider.messages[0][0]['content']
    assert ('sheet-helper instructions' in system) == ('sheet-helper' in expected)
    assert ('scan-helper instructions' in system) == ('scan-helper' in expected)
    assert loop.sessions.get_or_create('ui:chat').metadata['_recent_skills'] == expected
    # Follow-up reuses the immediately preceding task, never an older topic.
    loaded.clear()
    await loop._process_message(InboundMessage(channel='ui', sender_id='u', chat_id='chat', content='继续之前的任务',
                                               metadata={'run_mode': 'manual'}), audit_hook=audit)
    assert loaded == expected


def test_real_catalog_no_unrelated_matches_and_relevant_skills_still_route(tmp_path):
    import mira_engine.agent.skills as skills

    # Explicit directory disables external plugin discovery for a reproducible catalog.
    loader = SkillsLoader(tmp_path, builtin_skills_dir=Path(str(skills.BUILTIN_SKILLS_DIR)))
    for query in ['unrelatedquasar', '你好', '自我介绍', '请简单介绍一下你自己', 'Introduce yourself briefly']:
        assert loader.suggest_skills(query) == []
    for query, expected in [('Create a spreadsheet', 'xlsx'), ('systematic review', 'literature-review'),
                            ('Run ANOVA', 'statistical-analysis'), ('MRI MONAI', 'medical-image-analysis')]:
        assert expected in loader.suggest_skills(query)
    assert loader.suggest_skills('Create a spreadsheet') == ['xlsx']
    assert 'memory' in loader.get_always_skills()


def test_grammatical_words_alone_are_not_task_evidence(tmp_path):
    loader = make_loader(tmp_path)
    assert loader._score_skill_match(query_lc='a greeting with kindness',
        query_tokens=loader._tokenize('a greeting with kindness'), skill_name='sheet-helper',
        skill_text='a spreadsheet with formulas') == 0
