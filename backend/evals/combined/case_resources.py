"""Per-case Qdrant and Exercise Catalog fixture resources."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from evals.longitudinal.contract import ScenarioV2

_NAMESPACE = uuid.UUID("1d6c5ca2-1f5d-4ed4-8c2f-1af51c788504")


def _text(value: object, default: str = "") -> str:
    return " ".join(str(value or default).split())


def _authorized(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, Mapping) and item.get("authorized") is True]


def _row(item: Mapping[str, Any]) -> dict[str, Any]:
    exercise_id = _text(item.get("exercise_id") or item.get("id"))
    title = _text(item.get("title") or item.get("name"), exercise_id)
    indications = item.get("indications")
    conditions = indications if isinstance(indications, list) else [indications] if indications else []
    row = {
        "exercise_id": exercise_id,
        "slug": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or exercise_id,
        "title": title,
        "introduction": _text(item.get("introduction") or item.get("description") or item.get("content")),
        "structures_involved": [_text(value) for value in item.get("structures_involved", [])] if isinstance(item.get("structures_involved"), list) else [_text(item.get("structures_involved"))] if item.get("structures_involved") else [],
        "related_conditions": [_text(value) for value in conditions],
        "source_url": _text(item.get("source_url") or item.get("provenance"), f"evaluator://catalog/{exercise_id}"),
        "video_url": _text(item.get("video_url")),
        "video_provider": _text(item.get("video_provider")),
        "video_id": _text(item.get("video_id")),
    }
    for key in ("difficulty", "duration_seconds", "instructions", "contraindications", "version", "provenance", "media"):
        if key in item:
            row[key] = item[key]
    return row


@dataclass
class CaseResources:
    run_id: str
    case_id: str
    collection_name: str
    catalog_path: Path
    qdrant_path: Path | None = None
    evidence_items: tuple[dict[str, Any], ...] = ()
    catalog_items: tuple[dict[str, Any], ...] = ()
    store: Any = field(default=None, repr=False, compare=False)

    @property
    def authorized_evidence_ids(self) -> tuple[str, ...]:
        return tuple(_text(item.get("id")) for item in self.evidence_items)

    @property
    def authorized_catalog_ids(self) -> tuple[str, ...]:
        return tuple(_text(item.get("exercise_id") or item.get("id")) for item in self.catalog_items)

    def activation_environment(self) -> dict[str, str]:
        environment = {
            "QDRANT_COLLECTION_NAME": self.collection_name,
            "EXERCISE_CATALOG_PATH": str(self.catalog_path),
        }
        if self.qdrant_path is not None:
            environment["QDRANT_PATH"] = str(self.qdrant_path)
        return environment

    def _point_id(self, kind: str, logical_id: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.run_id}:{self.case_id}:{kind}:{logical_id}"))

    def provision(self, *, store_factory: Callable[..., Any] | None = None) -> dict[str, Any]:
        from app.vector.qdrant_store import KnowledgeDocument, QdrantKnowledgeStore

        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [_row(item) for item in self.catalog_items]
        self.catalog_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        if self.qdrant_path is not None:
            self.qdrant_path.mkdir(parents=True, exist_ok=True)
        if store_factory is None:
            self.store = QdrantKnowledgeStore(
                collection_name=self.collection_name,
                qdrant_path=str(self.qdrant_path) if self.qdrant_path is not None else None,
            )
        else:
            self.store = store_factory(collection_name=self.collection_name)
        self.store.ensure_collection(recreate=True)
        docs = []
        for item in self.evidence_items:
            source_id = _text(item.get("id"))
            docs.append(KnowledgeDocument(self._point_id("evidence", source_id), _text(item.get("content")), {"document_type": "evaluator_evidence", "source_id": source_id, "authorized": True, "recorded_at": _text(item.get("recorded_at"))}))
        for item in self.catalog_items:
            row = _row(item)
            docs.append(KnowledgeDocument(self._point_id("catalog", row["exercise_id"]), "\n".join(f"{key}: {value}" for key, value in row.items() if value), {"document_type": "rehab_exercise", **row, "authorized": True}))
        if docs:
            self.store.upsert_documents(docs)
        self.verify_retrieval()
        if store_factory is None and self.qdrant_path is not None:
            from app.vector.qdrant_store import close_shared_qdrant_clients
            close_shared_qdrant_clients()
            self.store = None
        return {"status": "provisioned", "transport": "evaluator_case_resources", "evidence_count": len(self.evidence_items), "catalog_count": len(rows), "document_count": len(docs)}

    def verify_retrieval(self) -> None:
        if self.store is None:
            raise RuntimeError("case Qdrant store has not been provisioned")
        for item in self.evidence_items:
            wanted = _text(item.get("id"))
            hits = self.store.search(_text(item.get("content")), limit=20)
            if not any(str(hit.get("payload", {}).get("source_id")) == wanted for hit in hits if isinstance(hit, Mapping)):
                raise RuntimeError("authorized evaluator evidence was not retrieved through the normal adapter")
        for item in self.catalog_items:
            wanted = _text(item.get("exercise_id") or item.get("id"))
            hits = self.store.search(_text(item.get("content") or item.get("title") or wanted), limit=20)
            if not any(str(hit.get("payload", {}).get("exercise_id")) == wanted for hit in hits if isinstance(hit, Mapping)):
                raise RuntimeError("authorized evaluator catalog was not retrieved through the normal adapter")


    def verify_isolation_against(self, previous: "CaseResources") -> dict[str, Any]:
        if self.collection_name == previous.collection_name or self.catalog_path == previous.catalog_path:
            raise RuntimeError("case resources are not isolated")
        if self.qdrant_path is not None and previous.qdrant_path is not None:
            if self.qdrant_path == previous.qdrant_path:
                raise RuntimeError("case Qdrant paths are not isolated")
            return {"status": "passed", "transport": "qdrant_path_isolation", "previous_points_checked": 0}
        client = getattr(self.store, "client", None)
        checked = 0
        if client is not None and callable(getattr(client, "retrieve", None)):
            for kind, items in (("evidence", previous.evidence_items), ("catalog", previous.catalog_items)):
                for item in items:
                    logical_id = _text(item.get("id") or item.get("exercise_id"))
                    points = client.retrieve(collection_name=self.collection_name, ids=[previous._point_id(kind, logical_id)], with_payload=False)
                    if points:
                        raise RuntimeError("a prior case Qdrant point is visible in the current collection")
                    checked += 1
        return {"status": "passed", "transport": "qdrant_exact_point_probe", "previous_points_checked": checked}

def build_case_resources(
    *,
    run_id: str,
    case_id: str,
    scenario: ScenarioV2 | None,
    catalog_dir: str | Path,
    qdrant_path: str | Path | None = None,
) -> CaseResources:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", case_id):
        raise ValueError("case id contains unsafe resource-name characters")
    collection = f"rehab_eval_{uuid.uuid5(_NAMESPACE, f'{run_id}:{case_id}').hex}"
    state = scenario.initial_state if scenario is not None else {}
    case_qdrant_path = (
        Path(qdrant_path).expanduser().resolve() / collection
        if qdrant_path is not None
        else None
    )
    return CaseResources(
        run_id, case_id, collection,
        Path(catalog_dir).expanduser().resolve() / f"{collection}.jsonl",
        case_qdrant_path,
        tuple(_authorized(state.get("evidence", []))),
        tuple(_authorized(state.get("catalog", []))),
    )
