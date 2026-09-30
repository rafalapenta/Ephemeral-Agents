"""Per-agent model registry for the LiteLLM handoff.

Resolves the right model for each domain agent, routing through the OmniRoute
gateway (OpenAI-compatible) instead of hitting vendor endpoints directly.

Why this exists
---------------
``handoff.py`` previously read a single global ``MODEL_NAME`` from ``.env`` and
passed it straight to ``litellm.completion``. Two failure modes resulted:

1. ``MODEL_PROVIDER=openai`` + ``MODEL_NAME=gpt-4o-mini`` hit ``api.openai.com``
   with the placeholder key from ``.env.example`` -> silent auth failure.
2. A bare provider-prefixed name (e.g. ``mistral/ministral-8b-latest``) is parsed
   by LiteLLM as *Mistral-the-vendor*, not as a model id on our gateway -> the
   request goes to ``api.mistral.ai`` and dies with an APIConnectionError.

This registry always emits an ``openai/<gateway-model-id>`` string plus an
explicit ``api_base``, which is the form LiteLLM routes to the gateway.

Configuration (all optional, env-driven)
----------------------------------------
``OMNIROUTE_BASE_URL``   gateway root, default ``http://localhost:20128/v1``
``OMNIROUTE_API_KEY``    bearer token for the gateway
``AGENT_MODEL_<AGENT>``  hard override for one agent, e.g. ``AGENT_MODEL_VULCAN``
``AGENT_MODEL_TIER``     ``reliable`` (default) | ``premium`` | ``fast``
``HANDOFF_TIMEOUT_SECONDS``  per-request timeout, default 120
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_TIMEOUT = 120.0
_DEFAULT_AGENT = "general"


@dataclass(frozen=True)
class ModelChoice:
    """One model candidate in a fallback chain."""

    model_id: str
    tier: str
    temperature: float = 0.2
    max_tokens: int = 4096
    note: str = ""


@dataclass(frozen=True)
class ResolvedModel:
    """A ready-to-send LiteLLM target for one agent."""

    agent_id: str
    model: str            # LiteLLM-addressable string, e.g. "openai/mistral/codestral-latest"
    api_base: str
    api_key: str
    temperature: float
    max_tokens: int
    timeout: float
    tier: str
    fallbacks: tuple[str, ...] = field(default_factory=tuple)
    source: str = "registry"


# ── Agent matrix ────────────────────────────────────────────────
# Order per agent is a FALLBACK CHAIN: the first entry is primary, the rest are
# used when the primary returns a rate-limit / connection error.
#
# Tier semantics:
#   premium  -> strongest reasoning. NOTE: on the shared gateway these are
#               rate-limited (429 free-models-per-day) and unusable without credits.
#   reliable -> verified live, sub-3s, no 429 under normal load.
#   fast     -> cheapest/fastest, for high-volume business & growth work.

AGENT_MODELS: dict[str, list[ModelChoice]] = {
    "atlas": [
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.2, 4096,
                    "orchestration / routing"),
        ModelChoice("mistral/ministral-8b-latest", "fast", 0.2, 4096),
    ],
    "vulcan": [
        ModelChoice("mistral/codestral-latest", "reliable", 0.1, 8192,
                    "code specialist: architecture, TDD, refactors"),
        ModelChoice("mistral/mistral-code-latest", "reliable", 0.1, 8192),
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.1, 4096),
    ],
    "lyra": [
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.2, 8192,
                    "evidence-first research & synthesis"),
        ModelChoice("mistral/magistral-medium-latest", "premium", 0.2, 8192,
                    "true reasoning tier — 429 without gateway credits"),
        ModelChoice("mistral/ministral-8b-latest", "fast", 0.2, 4096),
    ],
    "aura": [
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.4, 4096,
                    "product specs & UX copy"),
        ModelChoice("mistral/ministral-8b-latest", "fast", 0.4, 4096),
    ],
    "sterling": [
        ModelChoice("mistral/ministral-8b-latest", "fast", 0.2, 4096,
                    "fast + cost-effective for FP&A / operations"),
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.2, 4096),
    ],
    "vesper": [
        ModelChoice("mistral/ministral-8b-latest", "fast", 0.5, 4096,
                    "high-volume marketing & growth copy"),
        ModelChoice("mistral/ministral-14b-latest", "reliable", 0.5, 4096),
    ],
}

# Extra candidates appended when AGENT_MODEL_TIER=premium
PREMIUM_TAIL: list[ModelChoice] = [
    ModelChoice("mistral/magistral-medium-latest", "premium", 0.2, 8192),
    ModelChoice("mistral/mistral-medium-latest", "premium", 0.2, 8192),
]


def _gateway_base() -> str:
    return os.environ.get("OMNIROUTE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _gateway_key() -> str:
    # The gateway accepts requests without a bearer token, but sending one is
    # harmless and keeps us compatible with a locked-down deployment.
    return (
        os.environ.get("OMNIROUTE_API_KEY")
        or os.environ.get("HERMES_CUSTOM_OMNIROUTE_API_KEY")
        or os.environ.get("HERMES_CUSTOM_OMNIROUTE_2_API_KEY")
        or ""
    )


def _qualify(model_id: str, base: str) -> str:
    """Return a LiteLLM model string that resolves to *this gateway*.

    If the id already carries our provider prefix, keep it. If it carries a
    DIFFERENT provider prefix (``mistral/``, ``groq/``, ...) we must still wrap
    it in ``openai/`` — otherwise LiteLLM sends it to that vendor's API.
    """
    if model_id.startswith(("openai/", "hosted_vllm/", "azure/")):
        return model_id
    return f"openai/{model_id}"


def resolve_model(agent_id: str | None) -> ResolvedModel:
    """Resolve the LiteLLM target for *agent_id*.

    Never raises: an unknown or ``None`` agent falls back to the general chain,
    because a routing miss must not kill the handoff.
    """
    agent = (agent_id or _DEFAULT_AGENT).strip().lower()
    base = _gateway_base()
    timeout = float(os.environ.get("HANDOFF_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))

    # 1. Hard per-agent override wins over everything.
    override = os.environ.get(f"AGENT_MODEL_{agent.upper()}")
    if override:
        choice = ModelChoice(override, "override", 0.2, 4096, "AGENT_MODEL_* env override")
        chain = [choice]
        source = "env-override"
    else:
        chain = list(AGENT_MODELS.get(agent) or AGENT_MODELS.get("atlas", []))
        source = "registry"

    if not chain:
        chain = [ModelChoice("mistral/ministral-8b-latest", "reliable", 0.2, 4096, "default")]

    # 2. Optional tier promotion.
    if os.environ.get("AGENT_MODEL_TIER", "reliable").lower() == "premium":
        chain = [c for c in chain if c.tier != "fast"] + PREMIUM_TAIL

    primary, *rest = chain
    return ResolvedModel(
        agent_id=agent,
        model=_qualify(primary.model_id, base),
        api_base=base,
        api_key=_gateway_key(),
        temperature=primary.temperature,
        max_tokens=primary.max_tokens,
        timeout=timeout,
        tier=primary.tier,
        fallbacks=tuple(_qualify(c.model_id, base) for c in rest),
        source=source,
    )


def describe_matrix() -> list[dict[str, str]]:
    """Human-readable snapshot of the configured matrix (for diagnostics/CLI)."""
    return [
        {
            "agent": agent,
            "primary": chain[0].model_id,
            "tier": chain[0].tier,
            "fallback": chain[1].model_id if len(chain) > 1 else "-",
            "rationale": chain[0].note,
        }
        for agent, chain in sorted(AGENT_MODELS.items())
    ]


if __name__ == "__main__":  # pragma: no cover - diagnostics helper
    import json as _json

    print(_json.dumps(describe_matrix(), indent=2, ensure_ascii=False))