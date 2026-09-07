#!/usr/bin/env python3
"""Real AI chat integration and performance smoke test.

This script uses the actual Qwen API, Qdrant, PostgreSQL, and backend graph modules.
It is meant for staging or local development with real services running.

Usage from backend:
    python scripts/test_ai_chat_integration.py --ingest-if-empty
    python scripts/test_ai_chat_integration.py --recreate-ingest --ingest-limit 0
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
	sys.path.insert(0, str(BACKEND_DIR))


from app.core.config import load_backend_env

load_backend_env()
os.environ.setdefault("EMBEDDING_PROVIDER", "qwen")

from sqlalchemy import select

from app.ai.graph_execution import run_rehab_graph
from app.db.models import Patient
from app.db.session import SessionLocal, init_db
from app.vector import QdrantKnowledgeStore, load_rehab_exercises
from app.vector.qdrant_store import EmbeddingError

DEFAULT_INPUT = REPO_DIR / "misc" / "rehab_exercises_total.jsonl"


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run real AI chat integration checks")
	parser.add_argument("--message", default="I have mild knee stiffness after ACL rehab. What should I do today?")
	parser.add_argument("--search-query", default="knee stiffness ACL rehab squat")
	parser.add_argument("--patient-name", default="AI Chat Integration Patient")
	parser.add_argument("--ingest-if-empty", action="store_true", help="Ingest rehab exercises when Qdrant has no data")
	parser.add_argument("--recreate-ingest", action="store_true", help="Recreate Qdrant collection and ingest data before testing")
	parser.add_argument("--ingest-limit", type=int, default=50, help="Limit ingested docs. Zero means all docs.")
	parser.add_argument("--batch-size", type=int, default=16)
	parser.add_argument("--min-qdrant-points", type=int, default=1)
	parser.add_argument("--qdrant-path", default="data/qdrant_integration", help="Embedded Qdrant path fallback. Empty string uses QDRANT_URL.")
	return parser.parse_args()


def timed(label: str, func):
	start = time.perf_counter()
	result = func()
	elapsed = time.perf_counter() - start
	print(f"[PERF] {label}: {elapsed:.2f}s")
	return result, elapsed


def ensure_patient(patient_name: str) -> uuid.UUID:
	init_db()
	db = SessionLocal()
	try:
		patient = db.execute(select(Patient).where(Patient.patient_name == patient_name)).scalar_one_or_none()
		if patient is None:
			patient = Patient(
				patient_name=patient_name,
				password_hash="integration-test-not-for-login",
				real_info={"source": "test_ai_chat_integration"},
			)
			db.add(patient)
			db.commit()
			db.refresh(patient)
		return patient.patient_id
	finally:
		db.close()


def ingest_real_rehab_data(store: QdrantKnowledgeStore, *, recreate: bool, limit: int, batch_size: int) -> int:
	exercises = load_rehab_exercises(DEFAULT_INPUT)
	selected = exercises[:limit] if limit and limit > 0 else exercises
	docs = [exercise.to_document() for exercise in selected]
	store.ensure_collection(recreate=recreate)
	store.upsert_documents(docs, batch_size=batch_size)
	return len(docs)


def main() -> int:
	args = parse_args()
	if not os.getenv("QWEN_API_KEY"):
		print("[FAIL] QWEN_API_KEY is not set. Put it in root .env or legacy backend/.env, or export it before running.")
		return 2

	qdrant_path = args.qdrant_path or None
	store = QdrantKnowledgeStore(embedding_provider="qwen", qdrant_path=qdrant_path)
	if qdrant_path:
		print(f"[INFO] Using embedded Qdrant path: {qdrant_path}")
	else:
		print("[INFO] Using configured Qdrant URL")
	try:
		_, _ = timed("qwen embedding handshake", lambda: store.embed_text("knee rehab embedding connectivity check"))
	except EmbeddingError as exc:
		print(f"[FAIL] Qwen embedding API failed: {exc}")
		return 2

	try:
		if args.recreate_ingest:
			ingested, _ = timed(
				"qdrant real data ingest",
				lambda: ingest_real_rehab_data(store, recreate=True, limit=args.ingest_limit, batch_size=args.batch_size),
			)
			print(f"[OK] Ingested {ingested} real rehab exercise docs")

		count, _ = timed("qdrant count", store.count)
	except ValueError as exc:
		print(f"[FAIL] {exc}")
		print("[HINT] Run python scripts/test_ai_chat_integration.py --recreate-ingest --ingest-limit 0")
		return 2

	if count < args.min_qdrant_points:
		if not args.ingest_if_empty:
			print(f"[FAIL] Qdrant has {count} points. Re-run with --ingest-if-empty or --recreate-ingest.")
			return 2
		ingested, _ = timed(
			"qdrant real data ingest",
			lambda: ingest_real_rehab_data(store, recreate=False, limit=args.ingest_limit, batch_size=args.batch_size),
		)
		print(f"[OK] Ingested {ingested} real rehab exercise docs")
		count, _ = timed("qdrant count after ingest", store.count)

	print(f"[OK] Qdrant collection {store.collection_name} contains {count} points")
	results, _ = timed("qdrant semantic search", lambda: store.search(args.search_query, limit=3))
	if not results:
		print("[FAIL] Qdrant search returned no results")
		return 2
	for index, item in enumerate(results, start=1):
		payload = item.get("payload") or {}
		score = float(item.get("score") or 0.0)
		title = payload.get("title", "")
		print(f"  {index}. score={score:.4f} title={title}")

	if hasattr(store.client, "close"):
		store.client.close()

	patient_id, _ = timed("postgres test patient", lambda: ensure_patient(args.patient_name))
	print(f"[OK] Using patient_id={patient_id}")

	if qdrant_path:
		os.environ["QDRANT_PATH"] = qdrant_path
		from app.core.config import get_settings
		get_settings.cache_clear()
	graph_output, graph_elapsed = timed("run_rehab_graph end to end", lambda: run_rehab_graph(args.message, None, str(patient_id), principal_id=str(patient_id)))
	response = graph_output.get("final_response", "")
	print(f"[OK] Graph returned {len(response)} chars in {graph_elapsed:.2f}s")
	session_id = graph_output.get("session_id", "")
	tool_calling_used = graph_output.get("tool_calling_used")
	tool_calling_error = graph_output.get("tool_calling_error", "")
	print(f"[OK] Session: {session_id}")
	print(f"[OK] Tool calling used: {tool_calling_used}")
	print(f"[OK] Tool calling error: {tool_calling_error}")
	print("[RESPONSE]")
	print(response)
	return 0 if response else 2


if __name__ == "__main__":
	raise SystemExit(main())
