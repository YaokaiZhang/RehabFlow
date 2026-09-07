from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.vector import QdrantKnowledgeStore

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class EmbeddingCheckRequest(BaseModel):
	text: str = Field(default="knee stiffness ACL rehab", min_length=1, max_length=4000)


class QdrantSearchRequest(BaseModel):
	query: str = Field(default="knee stiffness ACL rehab squat", min_length=1, max_length=4000)
	limit: int = Field(default=3, ge=1, le=10)


def _close_store(store: QdrantKnowledgeStore) -> None:
	store.close()


@router.get("/config")
def diagnostics_config() -> dict[str, Any]:
	settings = get_settings()
	return {
		"environment": settings.environment,
		"qdrant_collection_name": settings.qdrant_collection_name,
		"qdrant_vector_size": settings.qdrant_vector_size,
		"qdrant_url": settings.qdrant_url,
		"qdrant_path_configured": bool(settings.qdrant_path),
		"embedding_provider": settings.embedding_provider,
		"embedding_model": settings.openai_embedding_model,
	}


@router.get("/db")
def diagnostics_db() -> dict[str, Any]:
	start = time.perf_counter()
	db = SessionLocal()
	try:
		value = db.execute(text("SELECT 1")).scalar_one()
		return {"status": "ok", "result": value, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
	except Exception as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Database check failed: {type(exc).__name__}: {exc}",
		) from exc
	finally:
		db.close()


@router.post("/embedding")
def diagnostics_embedding(payload: EmbeddingCheckRequest) -> dict[str, Any]:
	start = time.perf_counter()
	store = QdrantKnowledgeStore()
	try:
		vector = store.embed_text(payload.text)
		return {
			"status": "ok",
			"provider": get_settings().embedding_provider,
			"dimension": len(vector),
			"sample": vector[:5],
			"latency_ms": round((time.perf_counter() - start) * 1000, 2),
		}
	except Exception as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Embedding check failed: {type(exc).__name__}: {exc}",
		) from exc
	finally:
		_close_store(store)


@router.post("/qdrant/search")
def diagnostics_qdrant_search(payload: QdrantSearchRequest) -> dict[str, Any]:
	start = time.perf_counter()
	store = QdrantKnowledgeStore()
	try:
		count = store.count()
		results = store.search(payload.query, limit=payload.limit)
		return {
			"status": "ok",
			"collection": store.collection_name,
			"point_count": count,
			"latency_ms": round((time.perf_counter() - start) * 1000, 2),
			"results": results,
		}
	except Exception as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Qdrant search failed: {type(exc).__name__}: {exc}",
		) from exc
	finally:
		_close_store(store)
