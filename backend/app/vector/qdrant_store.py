from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Literal

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

from app.core.config import get_settings


EmbeddingProviderName = Literal["hash", "openai", "qwen"]


@dataclass(frozen=True)
class _SharedLocalClient:
    client: QdrantClient
    operation_lock: RLock


_LOCAL_CLIENTS: dict[str, _SharedLocalClient] = {}
_LOCAL_CLIENTS_LOCK = RLock()


def _shared_local_client(path: str) -> _SharedLocalClient:
    normalized_path = str(Path(path).expanduser().resolve())
    with _LOCAL_CLIENTS_LOCK:
        shared = _LOCAL_CLIENTS.get(normalized_path)
        if shared is None:
            shared = _SharedLocalClient(
                client=QdrantClient(
                    path=normalized_path,
                    force_disable_check_same_thread=True,
                ),
                operation_lock=RLock(),
            )
            _LOCAL_CLIENTS[normalized_path] = shared
        return shared


def close_shared_qdrant_clients() -> None:
    """Close process-wide embedded clients during application or test shutdown."""
    with _LOCAL_CLIENTS_LOCK:
        clients = tuple(_LOCAL_CLIENTS.values())
        _LOCAL_CLIENTS.clear()
    for shared in clients:
        with shared.operation_lock:
            shared.client.close()


@dataclass
class KnowledgeDocument:
	document_id: str
	text: str
	metadata: dict[str, Any]


class EmbeddingError(RuntimeError):
	pass


class BaseEmbedder:
	vector_size: int

	def embed_text(self, text: str) -> list[float]:
		raise NotImplementedError

	def embed_texts(self, texts: list[str]) -> list[list[float]]:
		return [self.embed_text(text) for text in texts]


class HashingEmbedder(BaseEmbedder):
	"""Deterministic local embedder for offline tests and development."""

	def __init__(self, vector_size: int) -> None:
		self.vector_size = vector_size

	def embed_text(self, text: str) -> list[float]:
		values = [0.0] * self.vector_size
		tokens = re.findall(r"[a-z0-9]+", text.lower())
		for token in tokens:
			digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
			bucket = int.from_bytes(digest[:4], "big") % self.vector_size
			sign = 1.0 if digest[4] & 1 else -1.0
			values[bucket] += sign

		norm = math.sqrt(sum(v * v for v in values)) or 1.0
		return [v / norm for v in values]


class OpenAIEmbeddingEmbedder(BaseEmbedder):
	"""OpenAI embeddings API client."""

	def __init__(
		self,
		*,
		api_key: str,
		base_url: str,
		model: str,
		vector_size: int,
		timeout_seconds: float = 60.0,
		max_retries: int = 3,
		embedding_batch_size: int = 10,
	) -> None:
		if not api_key:
			raise EmbeddingError("EMBEDDING_API_KEY or the provider-specific embedding API key is required")
		self.api_key = api_key
		self.base_url = base_url.rstrip("/")
		self.model = model
		self.vector_size = vector_size
		self.timeout_seconds = timeout_seconds
		self.max_retries = max_retries
		self.embedding_batch_size = max(1, embedding_batch_size)

	def embed_text(self, text: str) -> list[float]:
		return self.embed_texts([text])[0]

	def embed_texts(self, texts: list[str]) -> list[list[float]]:
		vectors: list[list[float]] = []
		for start in range(0, len(texts), self.embedding_batch_size):
			vectors.extend(self._embed_text_batch(texts[start : start + self.embedding_batch_size]))
		return vectors

	def _embed_text_batch(self, texts: list[str]) -> list[list[float]]:
		if not texts:
			return []

		url = f"{self.base_url}/embeddings"
		payload = {
			"model": self.model,
			"input": texts,
			"dimensions": self.vector_size,
			"encoding_format": "float",
		}
		headers = {
			"Authorization": f"Bearer {self.api_key}",
			"Content-Type": "application/json",
		}

		last_error: Exception | None = None
		for attempt in range(1, self.max_retries + 1):
			try:
				response = requests.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
				if response.status_code >= 400:
					raise EmbeddingError(f"OpenAI embedding request failed: {response.status_code} {response.text[:500]}")
				body = response.json()
				vectors = [item["embedding"] for item in sorted(body.get("data", []), key=lambda item: item.get("index", 0))]
				if len(vectors) != len(texts):
					raise EmbeddingError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
				for vector in vectors:
					if len(vector) != self.vector_size:
						raise EmbeddingError(f"Expected embedding dimension {self.vector_size}, got {len(vector)}")
				return vectors
			except Exception as exc:
				last_error = exc
				if attempt == self.max_retries:
					break
				time.sleep(min(2 ** attempt, 8))

		raise EmbeddingError(f"OpenAI embedding request failed after {self.max_retries} attempts: {last_error}")


QwenEmbeddingEmbedder = OpenAIEmbeddingEmbedder


def build_embedder(provider: EmbeddingProviderName | None = None, vector_size: int | None = None) -> BaseEmbedder:
	settings = get_settings()
	selected_provider = (provider or settings.embedding_provider).lower()
	selected_vector_size = vector_size or settings.qdrant_vector_size

	if selected_provider == "hash":
		return HashingEmbedder(vector_size=selected_vector_size)
	if selected_provider == "openai":
		return OpenAIEmbeddingEmbedder(
			api_key=settings.embedding_api_key or settings.openai_api_key or os.getenv("OPENAI_API_KEY", ""),
			base_url=settings.embedding_api_base or settings.openai_api_base,
			model=settings.embedding_model or settings.openai_embedding_model,
			vector_size=selected_vector_size,
			timeout_seconds=settings.embedding_timeout_seconds,
			max_retries=settings.embedding_max_retries,
			embedding_batch_size=settings.embedding_batch_size,
		)
	if selected_provider == "qwen":
		return QwenEmbeddingEmbedder(
			api_key=settings.embedding_api_key or settings.qwen_api_key or os.getenv("QWEN_API_KEY", ""),
			base_url=settings.embedding_api_base or settings.qwen_api_base,
			model=settings.embedding_model or settings.qwen_embedding_model,
			vector_size=selected_vector_size,
			timeout_seconds=settings.embedding_timeout_seconds,
			max_retries=settings.embedding_max_retries,
			embedding_batch_size=settings.embedding_batch_size,
		)
	raise ValueError(f"Unsupported embedding provider: {selected_provider}")


class QdrantKnowledgeStore:
	def __init__(
		self,
		collection_name: str | None = None,
		vector_size: int | None = None,
		qdrant_path: str | None = None,
		embedding_provider: EmbeddingProviderName | None = None,
	) -> None:
		settings = get_settings()
		self.collection_name = collection_name or settings.qdrant_collection_name
		self.vector_size = vector_size or settings.qdrant_vector_size
		self.embedder = build_embedder(embedding_provider, self.vector_size)
		self._operation_lock = RLock()
		self._uses_shared_local_client = False
		path = qdrant_path or settings.qdrant_path
		if path and path != ":memory:":
			shared = _shared_local_client(path)
			self.client = shared.client
			self._operation_lock = shared.operation_lock
			self._uses_shared_local_client = True
		elif path:
			self.client = QdrantClient(path=path)
		else:
			self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

	def ensure_collection(self, recreate: bool = False) -> None:
		with self._operation_lock:
			collections = self.client.get_collections().collections
			existing = {c.name for c in collections}
			if recreate and self.collection_name in existing:
				self.client.delete_collection(collection_name=self.collection_name)
				existing.remove(self.collection_name)

			if self.collection_name in existing:
				self._assert_collection_vector_size()
				return

			self.client.create_collection(
				collection_name=self.collection_name,
				vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
			)

	def _assert_collection_vector_size(self) -> None:
		with self._operation_lock:
			info = self.client.get_collection(collection_name=self.collection_name)
			vectors_config = info.config.params.vectors
			actual_size = getattr(vectors_config, "size", None)
			if actual_size is None and isinstance(vectors_config, dict):
				actual_size = next(iter(vectors_config.values())).size
			if actual_size != self.vector_size:
				raise ValueError(
					f"Qdrant collection {self.collection_name} has vector size {actual_size}, "
					f"but backend expects {self.vector_size}. Re-run ingestion with --recreate."
				)

	def upsert_documents(self, docs: list[KnowledgeDocument], batch_size: int = 128) -> None:
		for start in range(0, len(docs), batch_size):
			batch = docs[start : start + batch_size]
			vectors = self.embedder.embed_texts([doc.text for doc in batch])
			points = [
				PointStruct(
					id=doc.document_id,
					vector=vector,
					payload={"text": doc.text, **doc.metadata},
				)
				for doc, vector in zip(batch, vectors)
			]
			if points:
				with self._operation_lock:
					self.client.upsert(collection_name=self.collection_name, points=points)

	def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
		self.ensure_collection(recreate=False)
		query_vector = self.embed_text(query)
		with self._operation_lock:
			try:
				response = self.client.query_points(
					collection_name=self.collection_name,
					query=query_vector,
					limit=limit,
					with_payload=True,
				).points
			except AttributeError:
				response = self.client.search(
					collection_name=self.collection_name,
					query_vector=query_vector,
					limit=limit,
					with_payload=True,
				)
		return [{"score": item.score, "payload": item.payload} for item in response]

	def set_payload(self, point_id: str, payload: dict[str, Any]) -> None:
		with self._operation_lock:
			self.client.set_payload(
				collection_name=self.collection_name,
				payload=payload,
				points=[point_id],
			)

	def find_by_payload(self, key: str, value: str, limit: int = 10) -> list[dict[str, Any]]:
		with self._operation_lock:
			response, _ = self.client.scroll(
				collection_name=self.collection_name,
				scroll_filter=Filter(
					must=[FieldCondition(key=key, match=MatchValue(value=value))],
				),
				limit=limit,
				with_payload=True,
				with_vectors=False,
			)
		return [{"id": str(point.id), "payload": point.payload} for point in response]

	def count(self) -> int:
		self.ensure_collection(recreate=False)
		with self._operation_lock:
			return int(self.client.count(collection_name=self.collection_name, exact=True).count)

	def embed_text(self, text: str) -> list[float]:
		return self.embedder.embed_text(text)

	def close(self) -> None:
		if self._uses_shared_local_client:
			return
		with self._operation_lock:
			if hasattr(self.client, "close"):
				self.client.close()
