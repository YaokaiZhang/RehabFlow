"""Ingest rehab knowledge into vector DB.

Usage examples:
python scripts/ingest_knowledge.py --files ./data/knee.txt ./data/ankle.txt --body-part knee --difficulty beginner
python scripts/ingest_knowledge.py --urls https://example.org/rehab-knee --body-part knee --difficulty intermediate
"""

from __future__ import annotations

import argparse
import pathlib
import uuid

import requests

from app.vector import KnowledgeDocument, QdrantKnowledgeStore


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Ingest rehab knowledge data to vector DB")
	parser.add_argument("--files", nargs="*", default=[], help="Local text files to ingest")
	parser.add_argument("--urls", nargs="*", default=[], help="URLs to ingest as plain text")
	parser.add_argument("--body-part", type=str, required=True, help="Metadata: target body part")
	parser.add_argument("--difficulty", type=str, required=True, help="Metadata: difficulty level")
	parser.add_argument("--chunk-size", type=int, default=400, help="Chunk size in characters")
	parser.add_argument("--chunk-overlap", type=int, default=50, help="Chunk overlap in characters")
	return parser.parse_args()


def load_sources(file_paths: list[str], urls: list[str]) -> list[dict[str, str]]:
	sources: list[dict[str, str]] = []

	for fp in file_paths:
		path = pathlib.Path(fp)
		if not path.exists() or not path.is_file():
			print(f"[WARN] Skipping missing file: {fp}")
			continue
		text = path.read_text(encoding="utf-8", errors="ignore")
		sources.append({"source": str(path), "text": text})

	for url in urls:
		try:
			response = requests.get(url, timeout=15)
			response.raise_for_status()
			sources.append({"source": url, "text": response.text})
		except Exception as exc:  # noqa: BLE001
			print(f"[WARN] Failed to fetch {url}: {exc}")

	return sources


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
	text = " ".join(text.split())
	if not text:
		return []

	chunks: list[str] = []
	start = 0
	step = max(1, chunk_size - chunk_overlap)
	while start < len(text):
		chunks.append(text[start : start + chunk_size])
		start += step
	return chunks


def main() -> None:
	args = parse_args()
	sources = load_sources(args.files, args.urls)
	if not sources:
		print("[INFO] No valid sources found. Nothing to ingest.")
		return

	store = QdrantKnowledgeStore()
	store.ensure_collection()

	docs: list[KnowledgeDocument] = []
	for item in sources:
		chunks = chunk_text(item["text"], args.chunk_size, args.chunk_overlap)
		for idx, chunk in enumerate(chunks):
			docs.append(
				KnowledgeDocument(
					document_id=str(uuid.uuid4()),
					text=chunk,
					metadata={
						"source": item["source"],
						"chunk_index": idx,
						"body_part": args.body_part,
						"difficulty": args.difficulty,
						# TODO: Add richer metadata extraction (movement_type, contraindications, language).
					},
				)
			)

	store.upsert_documents(docs)
	print(f"[OK] Ingested {len(docs)} chunks into collection '{store.collection_name}'.")


if __name__ == "__main__":
	# TODO: Plug in production embedding model and batched embedding pipeline.
	main()
