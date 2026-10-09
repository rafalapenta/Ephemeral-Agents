"""Embeddings layer for Ephemeral-Agents catalog and semantic router.

Provides:
- LocalEmbeddingFunction: SentenceTransformers local offline embeddings (default: paraphrase-multilingual-MiniLM-L12-v2)
- GatewayEmbeddingFunction: OmniRoute / OpenAI compatible gateway embeddings
- DeterministicHashEmbeddingFunction: Blake2b deterministic hashing fallback with 0 dependencies
- get_embedding_function(): Factory resolving AGENCY_EMBEDDINGS (local | gateway | hash)
- Index metadata compatibility checker
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, Any] = {}
METADATA_FILENAME = "embedding_meta.json"


@runtime_checkable
class EmbeddingFunction(Protocol):
    """Protocol conforming to Chroma embedding function signature."""
    backend_name: str
    model_name: str

    def __call__(self, input: list[str]) -> list[list[float]]:
        ...


class DeterministicHashEmbeddingFunction:
    """Deterministic hash-based embedding function using blake2b.
    
    Works completely offline with 0 dependencies, fast and reproducible for tests.
    """

    backend_name: str = "hash"

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.model_name = f"blake2b-{dimensions}"

    def __call__(self, input: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for text_item in input:
            vec = [0.0] * self.dimensions
            tokens = re.findall(r"(?u)\b[\w-]+\b", text_item.lower()) or [text_item.lower()]
            for token in tokens:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dimensions
                weight = ((digest[4] % 100) + 1) / 50.0
                vec[idx] += weight
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]
            embeddings.append(vec)
        return embeddings


class LocalEmbeddingFunction:
    """Local embedding function using sentence-transformers (multilingual)."""

    backend_name: str = "local"

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self.model_name = model_name
        self._model = None

    def _get_model(self):
        if self._model is None:
            if self.model_name in _MODEL_CACHE:
                self._model = _MODEL_CACHE[self.model_name]
            else:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
                _MODEL_CACHE[self.model_name] = self._model
        return self._model

    def __call__(self, input: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(input, normalize_embeddings=True)
        return [v.tolist() for v in vectors]


class GatewayEmbeddingFunction:
    """Gateway embedding function using OmniRoute / OpenAI-compatible endpoint."""

    backend_name: str = "gateway"

    def __init__(
        self,
        model: str | None = None,
        dimensions: int = 1024,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model or os.getenv("AGENCY_GATEWAY_EMBEDDING_MODEL", "openai/mistral-embed")
        self.dimensions = dimensions
        self.api_base = (
            api_base
            or os.getenv("AGENCY_LLM_BASE_URL")
            or os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("AGENCY_LLM_API_KEY") or os.getenv("OMNIROUTE_API_KEY", "")

    def __call__(self, input: list[str]) -> list[list[float]]:
        import litellm
        try:
            res = litellm.embedding(
                model=self.model_name,
                input=input,
                api_base=self.api_base,
                api_key=self.api_key,
            )
            return [item["embedding"] for item in res.data]
        except Exception as e:
            logger.error("Gateway embedding failed: %s", e)
            return [[0.0] * self.dimensions for _ in input]


# Distinct alias for backwards compatibility
HashEmbeddingFunction = DeterministicHashEmbeddingFunction


def resolve_embedding_backend() -> str:
    """Resolve embedding backend from environment: local | gateway | hash (default: local)."""
    return os.getenv("AGENCY_EMBEDDINGS", "local").strip().lower()


def get_embedding_function(
    backend: str | None = None,
    model_name: str | None = None,
) -> Any:
    """Resolve the appropriate embedding function based on AGENCY_EMBEDDINGS.
    
    If 'local' is chosen and sentence-transformers is missing, gracefully falls back
    to 'hash' with a descriptive warning.
    """
    resolved = (backend or resolve_embedding_backend()).strip().lower()

    if resolved == "gateway":
        return GatewayEmbeddingFunction(model=model_name)
    elif resolved == "hash":
        return DeterministicHashEmbeddingFunction()
    elif resolved == "local":
        try:
            import sentence_transformers  # noqa: F401
            return LocalEmbeddingFunction(model_name=model_name or "paraphrase-multilingual-MiniLM-L12-v2")
        except ImportError:
            logger.warning(
                "sentence-transformers not installed; falling back to 'hash' embeddings. "
                "To enable local embeddings, install sentence-transformers (pip install 'aggency[local-embeddings]')."
            )
            return DeterministicHashEmbeddingFunction()
    else:
        logger.warning("Unknown AGENCY_EMBEDDINGS '%s'; falling back to 'hash'.", resolved)
        return DeterministicHashEmbeddingFunction()


def save_embedding_metadata(chroma_path: Path | str, embedding_fn: Any) -> None:
    """Persist embedding backend metadata alongside Chroma vectors."""
    path = Path(chroma_path)
    path.mkdir(parents=True, exist_ok=True)
    meta_file = path / METADATA_FILENAME
    data = {
        "backend": getattr(embedding_fn, "backend_name", "unknown"),
        "model": getattr(embedding_fn, "model_name", "unknown"),
    }
    meta_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def check_embedding_compatibility(chroma_path: Path | str, embedding_fn: Any | None = None) -> tuple[bool, str | None]:
    """Verify whether current embedding function matches index metadata."""
    if embedding_fn is None:
        embedding_fn = get_embedding_function()
    path = Path(chroma_path)
    meta_file = path / METADATA_FILENAME
    if not meta_file.exists():
        return True, None
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        stored_backend = data.get("backend")
        stored_model = data.get("model")
        current_backend = getattr(embedding_fn, "backend_name", "unknown")
        current_model = getattr(embedding_fn, "model_name", "unknown")

        if (stored_backend and stored_backend != current_backend) or (stored_model and stored_model != current_model):
            msg = (
                f"Aviso: O catálogo foi indexado com o backend '{stored_backend}' ({stored_model}), "
                f"mas a configuração atual é '{current_backend}' ({current_model}). "
                f"Vetores de modelos diferentes não são comparáveis; execute 'aggency-index --reindex' para atualizar o índice."
            )
            return False, msg
    except Exception as exc:
        logger.warning("Failed to read embedding metadata: %s", exc)
    return True, None
