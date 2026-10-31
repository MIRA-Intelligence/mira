from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.modules.tools import PubMedSearchTool
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.session.manager import SessionManager
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.models import ResearchReference
from mira_engine.workflows.service import WorkflowError


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "dummy/native"


def _service(tmp_path: Path) -> WorkflowService:
    project_id = "PRJ-initiation-literature-regression"
    project_dir = tmp_path / project_id
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": project_id}),
        encoding="utf-8",
    )
    return WorkflowService(JsonWorkflowRepository(project_dir, project_id))


def test_enabled_pubmed_research_rejects_unsearched_structured_result(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create",
        module_type="initiation",
        module_data={
            "intent": "review recent evidence",
            "literature_review": {"enabled": True, "sources": ["pubmed"]},
        },
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    run = service.repository.load_run(run_id)

    with pytest.raises(WorkflowError, match="no successful PubMed search"):
        service.command_run(
            run_id,
            command_id="premature-result",
            command_type="record_research_result",
            expected_revision=run.revision,
            payload={
                "research_result": {
                    "survey": "A model-memory summary without a search.",
                    "references": [{"id": "R1", "title": "Unverified paper"}],
                    "notes": [],
                    "gaps": [],
                    "directions": [],
                }
            },
        )


def test_pubmed_parser_returns_original_research_card_fields() -> None:
    articles = PubMedSearchTool._parse_articles(
        """
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>12345678</PMID>
              <Article>
                <ArticleTitle>AI staging of liver fibrosis</ArticleTitle>
                <Abstract><AbstractText>Multicenter validation abstract.</AbstractText></Abstract>
                <AuthorList>
                  <Author><LastName>Wang</LastName><Initials>C</Initials></Author>
                </AuthorList>
                <Journal>
                  <JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue>
                  <Title>Radiology</Title>
                </Journal>
              </Article>
            </MedlineCitation>
            <PubmedData>
              <ArticleIdList><ArticleId IdType="doi">10.1000/liver-ai</ArticleId></ArticleIdList>
            </PubmedData>
          </PubmedArticle>
        </PubmedArticleSet>
        """
    )

    assert articles == [
        {
            "pmid": "12345678",
            "title": "AI staging of liver fibrosis",
            "authors": "Wang C",
            "year": "2025",
            "venue": "Radiology",
            "doi": "10.1000/liver-ai",
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
            "abstract": "Multicenter validation abstract.",
            "source": "PubMed",
        }
    ]


def test_reference_splits_legacy_combined_pubmed_url_and_doi() -> None:
    reference = ResearchReference(
        id="R1",
        title="Verified paper",
        url="https://pubmed.ncbi.nlm.nih.gov/12345678/ (DOI: 10.1000/liver-ai)",
    )

    assert reference.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert reference.doi == "10.1000/liver-ai"


async def test_pubmed_tool_stops_after_six_audited_queries(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create",
        module_type="initiation",
        module_data={
            "intent": "review recent evidence",
            "literature_review": {"enabled": True, "sources": ["pubmed"]},
        },
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    for index in range(6):
        run = service.repository.load_run(run_id)
        service.command_run(
            run_id,
            command_id=f"search-{index}",
            command_type="record_literature_search",
            expected_revision=run.revision,
            payload={
                "search": {
                    "source": "pubmed",
                    "query": f"query {index}",
                    "status": "completed",
                    "result_count": 1,
                    "evidence_urls": [f"https://pubmed.ncbi.nlm.nih.gov/{index}/"],
                }
            },
        )
    tool = PubMedSearchTool()
    tool.set_project_context(service.repository.project_id, str(service.repository.project_dir))
    tool.set_workflow_run_context(run_id)

    result = await tool.execute(query="one query too many", count=10)

    assert result.startswith("Error: PubMed search budget reached (6 queries).")
    assert len(service.repository.load_run(run_id).module_data["research_searches"]) == 6


async def test_progress_only_reply_continues_into_real_pubmed_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create",
        module_type="initiation",
        module_data={
            "intent": "review AI liver fibrosis staging",
            "literature_review": {"enabled": True, "sources": ["pubmed"]},
        },
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    provider = _ScriptedProvider(
        [
            LLMResponse(content="正在按 PubMed 执行检索。"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="pubmed-call",
                        name="pubmed_search",
                        arguments={
                            "query": '("liver fibrosis"[Title/Abstract]) AND AI[Title/Abstract]',
                            "count": 10,
                        },
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="result-call",
                        name="record_initiation_research_result",
                        arguments={
                            "survey": "近年研究覆盖影像和临床多模态分级。",
                            "references": [
                                {
                                    "id": "PMID-12345678",
                                    "title": "AI staging of liver fibrosis",
                                    "authors": "Wang C",
                                    "year": "2025",
                                    "source": "PubMed",
                                    "venue": "Radiology",
                                    "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                                    "summary": "Multicenter validation.",
                                    "relevance": "Direct staging evidence.",
                                }
                            ],
                            "notes": ["外部验证仍不足。"],
                            "gaps": ["缺少跨病因泛化。"],
                            "directions": [
                                {
                                    "id": "direction-1",
                                    "title": "跨病因外部验证",
                                    "rationale": "直接检验泛化缺口。",
                                }
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="choice-call",
                        name="request_workflow_interaction",
                        arguments={
                            "interaction_type": "choose_direction",
                            "title": "选择下一步研究方向",
                            "prompt": "请选择希望继续探索的方向。",
                            "choices": [{"id": "direction-1", "label": "跨病因外部验证", "recommended": True}],
                            "allow_freeform": True,
                        },
                    )
                ],
            ),
            LLMResponse(content="文献调研结果已写入中间工作区，等待你选择方向。"),
        ]
    )
    loop = ResearchAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="dummy/native",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )
    pubmed_tool = loop.tools.get("pubmed_search")
    assert isinstance(pubmed_tool, PubMedSearchTool)

    async def fake_pubmed_search(*, query: str, count: int = 10) -> str:
        articles = [
            {
                "pmid": "12345678",
                "title": "AI staging of liver fibrosis",
                "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
            }
        ]
        pubmed_tool._record_search(query=query, status="completed", articles=articles)
        return json.dumps({"source": "PubMed", "returned": 1, "articles": articles})

    monkeypatch.setattr(pubmed_tool, "execute", fake_pubmed_search)

    response = await loop.process_direct(
        "请开始文献调研。",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
            "workflow_module_type": "initiation",
            "run_mode": "manual",
        },
    )

    run = service.repository.load_run(run_id)
    assert getattr(response, "content", "") == "等待你在交互表单中确认：选择下一步研究方向"
    # The durable form stops execution without another model turn.
    assert len(provider.responses) == 1
    assert len(provider.calls) == 4
    assert run.status == "waiting_input"
    assert run.module_data["research_searches"][0]["source"] == "pubmed"
    assert run.module_data["research_result"]["searches"][0]["query"].startswith(
        '("liver fibrosis"'
    )
    assert run.module_data["research_result"]["references"][0]["source"] == "PubMed"
    assert run.module_data["interaction"]["interaction_type"] == "choose_direction"
