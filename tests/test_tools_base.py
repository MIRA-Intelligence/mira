from __future__ import annotations

from typing import Any

import pytest

from medpilot.agent.tools.base import Tool


class _DummyTool(Tool):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "dummy tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 9},
                "enabled": {"type": "boolean"},
                "items": {"type": "array", "items": {"type": "number"}},
                "meta": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": 2}},
                    "required": ["name"],
                },
                "mode": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["count", "meta"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_cast_params_handles_nested_and_scalars() -> None:
    tool = _DummyTool()
    casted = tool.cast_params(
        {
            "count": "3",
            "enabled": "yes",
            "items": ["1.2", "3"],
            "meta": {"name": 123},
            "extra": "keep",
        }
    )
    assert casted["count"] == 3
    assert casted["enabled"] is True
    assert casted["items"] == [1.2, 3.0]
    assert casted["meta"]["name"] == "123"
    assert casted["extra"] == "keep"


def test_validate_params_reports_required_type_and_bounds() -> None:
    tool = _DummyTool()
    errors = tool.validate_params(
        {
            "count": 0,
            "enabled": "x",
            "items": [1, "bad"],
            "meta": {},
            "mode": "z",
        }
    )
    joined = " | ".join(errors)
    assert "count must be >= 1" in joined
    assert "enabled should be boolean" in joined
    assert "items[1] should be number" in joined
    assert "missing required meta.name" in joined
    assert "mode must be one of ['a', 'b']" in joined


def test_validate_params_rejects_non_object_input() -> None:
    tool = _DummyTool()
    assert tool.validate_params("nope") == ["parameters must be an object, got str"]


def test_validate_params_requires_object_schema() -> None:
    class _BadSchemaTool(_DummyTool):
        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "string"}

    with pytest.raises(ValueError, match="Schema must be object type"):
        _BadSchemaTool().validate_params({})


def test_to_schema_includes_function_fields() -> None:
    schema = _DummyTool().to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "dummy"
    assert schema["function"]["description"] == "dummy tool"
