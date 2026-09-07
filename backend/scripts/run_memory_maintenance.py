from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.services.memory_agent import process_queued_memory_maintenance


def main() -> None:
	with SessionLocal() as db:
		processed = process_queued_memory_maintenance(db)
	print(f"memory maintenance processed {processed} queued runs")


if __name__ == "__main__":
	main()
