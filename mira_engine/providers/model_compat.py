r"""Model-level compatibility flags shared across providers.

Some newer LLMs (Anthropic Claude Opus 4-7, several OpenAI reasoning
deployments, etc.) reject the ``temperature`` parameter outright. The
property is **model-scoped**, not provider-scoped: the same model is
exposed by Anthropic directly, by Azure AI's Anthropic deployment, by
AiHubMix/OpenRouter gateways, etc., and every path must drop the
parameter or the request 400s.

Keeping the rule in one place avoids the failure mode we observed in
production where ``azure/anthropic/claude-opus-4-7`` repeatedly errored
with ``\`temperature\` is deprecated for this model.`` while every
provider builder still attached ``temperature``.

To extend, add a substring token to ``TEMPERATURE_UNSUPPORTED_MODEL_TOKENS``.
"""

from __future__ import annotations

# Substrings (case-insensitive) that identify models which reject
# `temperature`. Match is intentionally loose so it catches the model
# under every provider/gateway prefix (``anthropic/...``,
# ``azure/anthropic/...``, ``openrouter/anthropic/...``, etc.).
TEMPERATURE_UNSUPPORTED_MODEL_TOKENS: frozenset[str] = frozenset(
    {
        # Claude Opus 4.x on Azure AI rejects `temperature` with
        # `invalid_request_error: \`temperature\` is deprecated for this model.`
        # The same model on the native Anthropic API still accepts it today,
        # but stripping it everywhere is safe (Anthropic defaults to 1.0).
        "claude-opus-4-7",
    }
)


def model_supports_temperature(model: str | None) -> bool:
    """Return True when the model is expected to accept ``temperature``.

    Empty / unknown model strings default to True so we don't accidentally
    suppress the parameter for ordinary models.
    """
    if not model:
        return True
    lowered = model.lower()
    return not any(token in lowered for token in TEMPERATURE_UNSUPPORTED_MODEL_TOKENS)
