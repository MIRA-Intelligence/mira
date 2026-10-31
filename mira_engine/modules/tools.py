"""Agent-facing tools that emit validated workflow executor signals."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from mira_engine.agent.tools.base import Tool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.models import new_id


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline a pydantic schema's `$defs` so it can be nested in a tool's parameters.

    Pydantic puts nested models in a root-level `$defs` and points at them with
    `#/$defs/...`. Nested under another object that root is gone, the pointers
    dangle and strict providers reject the whole request. Models must not be
    recursive.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                rest = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**defs[ref.removeprefix("#/$defs/")], **rest})
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


class PubMedSearchTool(Tool):
    """Search PubMed directly and persist a non-hallucinable search audit."""

    _EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    _MAX_SEARCHES_PER_RUN = 6
    _MAX_ABSTRACT_CHARS = 2000

    def __init__(self, *, proxy: str | None = None) -> None:
        self.proxy = proxy
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"pubmed_search_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "pubmed_search"

    @property
    def description(self) -> str:
        return (
            "Search NCBI PubMed and return verifiable article metadata and abstracts. "
            "Inside an Initiation Run this also records the actual query and PubMed URLs "
            "for the research audit. Use this whenever PubMed is selected."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PubMed query, including date or field filters when needed.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum articles to fetch.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    @staticmethod
    def _text(node: ElementTree.Element | None) -> str:
        return "".join(node.itertext()).strip() if node is not None else ""

    @classmethod
    def _parse_articles(cls, xml_text: str) -> list[dict[str, Any]]:
        root = ElementTree.fromstring(xml_text)
        articles: list[dict[str, Any]] = []
        for item in root.findall(".//PubmedArticle"):
            citation = item.find("./MedlineCitation")
            article = citation.find("./Article") if citation is not None else None
            pmid = cls._text(citation.find("./PMID") if citation is not None else None)
            if article is None or not pmid:
                continue
            author_names: list[str] = []
            for author in article.findall("./AuthorList/Author"):
                collective = cls._text(author.find("./CollectiveName"))
                if collective:
                    author_names.append(collective)
                    continue
                last = cls._text(author.find("./LastName"))
                initials = cls._text(author.find("./Initials"))
                name = " ".join(part for part in (last, initials) if part)
                if name:
                    author_names.append(name)
            pub_date = article.find("./Journal/JournalIssue/PubDate")
            year = cls._text(pub_date.find("./Year") if pub_date is not None else None)
            if not year and pub_date is not None:
                year = cls._text(pub_date.find("./MedlineDate")).split(" ", 1)[0]
            abstract = "\n".join(
                cls._text(part)
                for part in article.findall("./Abstract/AbstractText")
                if cls._text(part)
            )[: cls._MAX_ABSTRACT_CHARS]
            doi = ""
            for article_id in item.findall("./PubmedData/ArticleIdList/ArticleId"):
                if article_id.attrib.get("IdType") == "doi":
                    doi = cls._text(article_id)
                    break
            articles.append(
                {
                    "pmid": pmid,
                    "title": cls._text(article.find("./ArticleTitle")),
                    "authors": ", ".join(author_names),
                    "year": year,
                    "venue": cls._text(article.find("./Journal/Title")),
                    "doi": doi or None,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "abstract": abstract,
                    "source": "PubMed",
                }
            )
        return articles

    def _record_search(
        self,
        *,
        query: str,
        status: str,
        articles: list[dict[str, Any]],
        detail: str | None = None,
    ) -> None:
        context = self._context.get()
        if context is None or not context[2]:
            return
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.module_type != "initiation" or run.status not in {"running", "completed"}:
            return
        service.command_run(
            run_id,
            command_id=f"executor-literature-search:{new_id()}",
            command_type="record_literature_search",
            expected_revision=run.revision,
            payload={
                "search": {
                    "source": "pubmed",
                    "query": query,
                    "status": status,
                    "result_count": len(articles),
                    "evidence_urls": [article["url"] for article in articles],
                    "detail": detail,
                }
            },
        )

    def _run_search_count(self) -> int:
        context = self._context.get()
        if context is None or not context[2]:
            return 0
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        searches = run.module_data.get("research_searches")
        return len(searches) if isinstance(searches, list) else 0

    async def execute(self, *, query: str, count: int = 10) -> str:
        query = query.strip()
        count = min(max(int(count), 1), 10)
        if not query:
            return "Error: PubMed query must not be empty"
        if self._run_search_count() >= self._MAX_SEARCHES_PER_RUN:
            return (
                "Error: PubMed search budget reached (6 queries). Synthesize the "
                "evidence already returned and record the structured research result."
            )
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=30.0) as client:
                search_response = await client.get(
                    f"{self._EUTILS}/esearch.fcgi",
                    params={
                        "db": "pubmed",
                        "retmode": "json",
                        "sort": "date",
                        "retmax": count,
                        "term": query,
                        "tool": "mira",
                        "email": "support@mira-intelligence.com",
                    },
                )
                search_response.raise_for_status()
                search_result = search_response.json().get("esearchresult") or {}
                pmids = [
                    str(pmid)
                    for pmid in (search_result.get("idlist") or [])
                    if str(pmid).strip()
                ]
                articles: list[dict[str, Any]] = []
                if pmids:
                    fetch_response = await client.get(
                        f"{self._EUTILS}/efetch.fcgi",
                        params={
                            "db": "pubmed",
                            "retmode": "xml",
                            "rettype": "abstract",
                            "id": ",".join(pmids),
                            "tool": "mira",
                            "email": "support@mira-intelligence.com",
                        },
                    )
                    fetch_response.raise_for_status()
                    articles = self._parse_articles(fetch_response.text)
            self._record_search(query=query, status="completed", articles=articles)
            return json.dumps(
                {
                    "source": "PubMed",
                    "query": query,
                    "total_matches": int(search_result.get("count") or 0),
                    "returned": len(articles),
                    "articles": articles,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._record_search(
                query=query,
                status="failed",
                articles=[],
                detail=detail,
            )
            return f"Error: PubMed search failed: {detail}"


class WorkflowInteractionTool(Tool):
    """Turn an AI decision point into durable Run state and a UI widget."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_interaction_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "request_workflow_interaction"

    @property
    def description(self) -> str:
        return (
            "Pause the active MIRA ModuleRun at a durable checkpoint and ask the user "
            "one structured question at ANY stage, repeatedly as new decisions arise. "
            "Use clarify_requirements for missing objectives or deliverables, resolve_conflict "
            "for incompatible user requirements, approve_* for execution approval, and "
            "choose_direction only for researched candidate directions. Explain why the answer "
            "matters, offer concise choices with trade-offs and a recommended option, and allow "
            "free-form input. Reuse saved answers unless new evidence changes the decision. After "
            "calling it, briefly say that the Run is waiting and end the turn."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interaction_type": {
                    "type": "string",
                    "description": "clarify_requirements, resolve_conflict, choose_direction, or a module's approve_* type.",
                },
                "title": {"type": "string", "maxLength": 160},
                "prompt": {"type": "string", "maxLength": 4000},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["id", "label"],
                    },
                    "maxItems": 12,
                },
                "allow_freeform": {"type": "boolean"},
            },
            "required": ["interaction_type", "title", "prompt", "choices", "allow_freeform"],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        run_id = current[2] if current else None
        self._context.set((project_id, Path(project_dir), run_id))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        interaction_type: str,
        title: str,
        prompt: str,
        choices: list[dict[str, Any]],
        allow_freeform: bool,
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: request_workflow_interaction is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.status != "running" or not run.attempt.current_attempt_id:
            return f"Error: ModuleRun is not actively running (status={run.status})"
        if (
            run.module_type == "initiation"
            and interaction_type == "choose_direction"
            and not isinstance(run.module_data.get("research_result"), dict)
        ):
            return (
                "Error: record_initiation_research_result must succeed before "
                "requesting a research-direction choice"
            )
        normalized_choices = []
        for choice in choices:
            normalized_choices.append(
                {
                    "id": str(choice["id"]),
                    "label": str(choice["label"]),
                    **(
                        {"description": str(choice["description"])}
                        if choice.get("description")
                        else {}
                    ),
                    "value": str(choice.get("value") or choice["id"]),
                }
            )
        interaction_id = new_id()
        selection_schema: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 4000}
        if interaction_type.startswith("approve_"):
            selection_schema["enum"] = ["approve", "revise"]
        elif not allow_freeform:
            if not normalized_choices:
                return "Error: provide choices or enable allow_freeform so the user can answer"
            selection_schema["enum"] = [choice["value"] for choice in normalized_choices]
        receipt = service.request_interaction(
            run_id,
            command_id=f"executor-interaction:{interaction_id}",
            attempt_id=run.attempt.current_attempt_id,
            checkpoint_state={"interaction_type": interaction_type},
            interaction={
                "interaction_id": interaction_id,
                "interaction_type": interaction_type,
                "title": title,
                "prompt": prompt,
                "response_schema": {
                    "type": "object",
                    "required": ["selection"],
                    "properties": {
                        "selection": selection_schema,
                        "notes": {"type": "string", "maxLength": 4000},
                    },
                    "additionalProperties": False,
                },
                "choices": normalized_choices,
                "allow_freeform": allow_freeform,
            },
        )
        return json.dumps(
            {
                "status": "waiting_input",
                "interaction_id": interaction_id,
                "checkpoint_id": receipt.event.payload.get("checkpoint_id")
                if receipt.event
                else None,
                "instruction": "End this turn and wait for the user's structured response.",
            },
            ensure_ascii=False,
        )


class WorkflowArtifactTool(Tool):
    """Publish one Project-relative file as an immutable workflow Artifact."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_artifact_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "publish_workflow_artifact"

    @property
    def description(self) -> str:
        return (
            "Publish a completed Project-relative file as an immutable Artifact "
            "owned by the active ModuleRun. Returns its Artifact ID."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "artifact_type": {"type": "string"},
                "label": {"type": "string"},
                "media_type": {"type": "string"},
            },
            "required": ["source_path", "artifact_type", "label", "media_type"],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        source_path: str,
        artifact_type: str,
        label: str,
        media_type: str,
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: publish_workflow_artifact is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        # A completed Run may publish revised deliverables in a follow-up turn.
        if run.status not in {"running", "completed"}:
            return f"Error: ModuleRun is not actively running (status={run.status})"
        artifact, event = service.import_artifact(
            command_id=f"executor-artifact:{new_id()}",
            source_path=source_path,
            artifact_type=artifact_type,
            label=label,
            media_type=media_type,
            derived_from=[item.artifact_id for item in run.inputs],
        )
        return json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "label": artifact.label,
                "project_sequence": event.project_sequence,
            },
            ensure_ascii=False,
        )


class InitiationResearchResultTool(Tool):
    """Persist a validated, UI-renderable literature research result."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"initiation_research_result_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "record_initiation_research_result"

    @property
    def description(self) -> str:
        return (
            "Record the current Initiation Run's structured literature survey, "
            "references, notes, research gaps, and candidate directions so the UI "
            "can render an auditable research result. Call this after evidence "
            "synthesis and before asking the user to choose a direction."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        reference_properties = {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "authors": {"type": "string"},
            "year": {"type": "string"},
            "source": {"type": "string"},
            "venue": {"type": "string"},
            "url": {"type": "string"},
            "doi": {"type": "string"},
            "summary": {"type": "string"},
            "relevance": {"type": "string"},
        }
        direction_properties = {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "tradeoffs": {"type": "string"},
            "next_step": {"type": "string"},
        }
        return {
            "type": "object",
            "properties": {
                "survey": {"type": "string"},
                "references": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": reference_properties,
                        "required": ["id", "title"],
                        "additionalProperties": False,
                    },
                },
                "notes": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "directions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": direction_properties,
                        "required": ["id", "title", "rationale"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["survey", "references", "notes", "gaps", "directions"],
            "additionalProperties": False,
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        survey: str,
        references: list[dict[str, Any]],
        notes: list[str],
        gaps: list[str],
        directions: list[dict[str, Any]],
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: record_initiation_research_result is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.module_type != "initiation" or run.status not in {"running", "completed"}:
            return (
                "Error: record_initiation_research_result requires an actively "
                f"running initiation Run (module={run.module_type}, status={run.status})"
            )
        receipt = service.command_run(
            run_id,
            command_id=f"executor-research-result:{new_id()}",
            command_type="record_research_result",
            expected_revision=run.revision,
            payload={
                "research_result": {
                    "schema_version": 1,
                    "survey": survey,
                    "references": references,
                    "notes": notes,
                    "gaps": gaps,
                    "directions": directions,
                }
            },
        )
        return json.dumps(
            {
                "status": "recorded",
                "run_id": run_id,
                "run_revision": receipt.run_revision,
                "project_sequence": receipt.project_sequence,
            },
            ensure_ascii=False,
        )


class WorkflowExperimentTool(InitiationResearchResultTool):
    """Upsert a Run-local experiment without using the legacy plan workflow."""

    @property
    def name(self) -> str:
        return "record_workflow_experiment"

    @property
    def description(self) -> str:
        return (
            "Register or update one experiment in the active Experiment Run. "
            "Use a stable local id (e.g. Exp001; follow-ups Exp001.a with parent Exp001). "
            "Record pending/running before execution, then update the same id with "
            "completed/failed/skipped, methods, metrics, findings and project-relative "
            "artifact paths. After completed/failed, record replan on the same id "
            "before starting another experiment. "
            "Omitted fields retain their previous values. The UI experiment list "
            "and detail panel update from these records."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        from mira_engine.workflows.experiments import ExperimentRecord

        return {
            "type": "object",
            "properties": {"experiment": _inline_refs(ExperimentRecord.model_json_schema())},
            "required": ["experiment"], "additionalProperties": False,
        }

    async def execute(self, *, experiment: dict[str, Any]) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: record_workflow_experiment is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.module_type != "experiment" or run.status != "running":
            return "Error: record_workflow_experiment requires an actively running experiment Run"
        receipt = service.command_run(
            run_id, command_id=f"executor-experiment:{new_id()}",
            command_type="record_experiment", expected_revision=run.revision,
            payload={"experiment": experiment},
        )
        return json.dumps({"status": "recorded", "run_id": run_id,
                           "experiment_id": experiment["id"], "run_revision": receipt.run_revision,
                           "project_sequence": receipt.project_sequence})


class WorkflowCompletionTool(Tool):
    """Commit a native executor's final outputs and optional handoff."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_completion_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "complete_workflow_run"

    @property
    def description(self) -> str:
        return (
            "Complete the active ModuleRun only after its requested outcome is "
            "finished. Pass immutable output Artifact IDs and explicit downstream "
            "suggestions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "maxLength": 4000},
                "output_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "suggested_handoffs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_module_type": {
                                "type": "string",
                                "enum": [
                                    "initiation",
                                    "planning",
                                    "experiment",
                                    "delivery",
                                ],
                            },
                            "input_artifact_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "summary": {"type": "string"},
                        },
                        "required": [
                            "target_module_type",
                            "input_artifact_ids",
                        ],
                    },
                },
            },
            "required": [
                "summary",
                "output_artifact_ids",
                "suggested_handoffs",
            ],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        summary: str,
        output_artifact_ids: list[str],
        suggested_handoffs: list[dict[str, Any]],
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: complete_workflow_run is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        literature = run.module_data.get("literature_review")
        if (
            run.module_type == "initiation"
            and isinstance(literature, dict)
            and literature.get("enabled") is True
            and not isinstance(run.module_data.get("research_result"), dict)
        ):
            return (
                "Error: enabled literature research must be recorded with "
                "record_initiation_research_result before completing the Run"
            )
        revising = run.status == "completed"
        receipt = service.command_run(
            run_id,
            command_id=f"executor-complete:{new_id()}",
            # A follow-up turn on a completed Run only replaces its deliverables.
            command_type="revise_outputs" if revising else "complete",
            expected_revision=run.revision,
            payload={
                "summary": summary,
                "outputs": output_artifact_ids,
                **({} if revising else {"suggested_handoffs": suggested_handoffs}),
            },
        )
        return json.dumps(
            {
                "status": "completed",
                "deliverables_revised": revising,
                "run_id": run_id,
                "project_sequence": receipt.project_sequence,
                "instruction": "End this turn without more tool calls.",
            },
            ensure_ascii=False,
        )
