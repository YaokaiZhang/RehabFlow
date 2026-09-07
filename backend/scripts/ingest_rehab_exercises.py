"""Normalize and ingest Rehab Hero exercise JSONL data into Qdrant.

Run from backend:
python scripts/ingest_rehab_exercises.py --embedding-provider qwen --recreate
python scripts/ingest_rehab_exercises.py --embedding-provider hash --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.vector import QdrantKnowledgeStore, load_rehab_exercises


DEFAULT_INPUT = Path(__file__).resolve().parents[2] / "misc" / "rehab_exercises_total.jsonl"
DEFAULT_NORMALIZED_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "rehab_exercises.normalized.jsonl"


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Ingest structured rehab exercise data into Qdrant")
	parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Source exercise JSONL file")
	parser.add_argument(
		"--normalized-output",
		type=Path,
		default=DEFAULT_NORMALIZED_OUTPUT,
		help="Where to write normalized and deduplicated JSONL",
	)
	parser.add_argument("--batch-size", type=int, default=32, help="Qdrant upsert batch size")
	parser.add_argument("--qdrant-path", type=str, default=None, help="Use local persisted Qdrant storage at this path")
	parser.add_argument("--embedding-provider", choices=["qwen", "hash"], default=None, help="Override embedding provider")
	parser.add_argument("--limit", type=int, default=0, help="Limit documents for smoke testing. Zero means all.")
	parser.add_argument("--recreate", action="store_true", help="Delete and recreate the Qdrant collection first")
	parser.add_argument("--dry-run", action="store_true", help="Normalize and report, but do not write to Qdrant")
	return parser.parse_args()


def write_normalized_jsonl(path: Path, exercises) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8") as handle:
		for exercise in exercises:
			handle.write(json.dumps(exercise.to_payload(), ensure_ascii=False, sort_keys=True))
			handle.write("\n")


def main() -> None:
	args = parse_args()
	exercises = load_rehab_exercises(args.input)
	write_normalized_jsonl(args.normalized_output, exercises)
	selected_exercises = exercises[: args.limit] if args.limit and args.limit > 0 else exercises
	docs = [exercise.to_document() for exercise in selected_exercises]

	without_structures = sum(1 for exercise in exercises if not exercise.structures_involved)
	without_conditions = sum(1 for exercise in exercises if not exercise.related_conditions)
	print(f"[OK] Normalized {len(exercises)} unique exercises from {args.input}")
	print(f"[OK] Wrote normalized JSONL to {args.normalized_output}")
	print(f"[INFO] Missing structures: {without_structures}; missing conditions: {without_conditions}")
	print(f"[INFO] Selected {len(docs)} documents for Qdrant ingestion")

	if args.dry_run:
		print("[DRY-RUN] Skipped Qdrant upsert.")
		return

	store = QdrantKnowledgeStore(qdrant_path=args.qdrant_path, embedding_provider=args.embedding_provider)
	store.ensure_collection(recreate=args.recreate)
	store.upsert_documents(docs, batch_size=args.batch_size)
	count = store.count()
	print(f"[OK] Upserted {len(docs)} exercise points into collection {store.collection_name}.")
	print(f"[OK] Collection now contains {count} points.")
	results = store.search("knee pain squat rehabilitation", limit=3)
	print(f"[OK] Verification search returned {len(results)} results.")
	for index, item in enumerate(results, start=1):
		payload = item.get("payload") or {}
		score = float(item.get("score") or 0.0)
		title = payload.get("title", "")
		print(f"  {index}. score={score:.4f} title={title}")


if __name__ == "__main__":
	main()
