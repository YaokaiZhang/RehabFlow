"""Injected evidence adapters with isolated resource scopes."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from sqlalchemy import select

from app.ai.context_assembler import (
    cards_from_memory_documents,
    cards_from_rehab_session,
    cards_from_triage_summary,
    latest_triage_summary_for_episode,
)
from app.ai.context_integrity import ContextIntegrity, PUBLIC_REHAB_KNOWLEDGE_SCOPE
from app.ai.context_types import ContextPacket
from app.ai.evidence_types import EvidenceRequest, EvidenceResult, EvidenceSource
from app.db.models import AIMessage, AISession, CareEpisode, MemoryDocument, ai_message_ordering
from app.services.rehab_session_context import latest_rehab_session_for_episode


class EvidenceExecutionContext(Protocol):
    context_integrity: ContextIntegrity


@dataclass(frozen=True)
class DefaultEvidenceExecutionContext:
    context_integrity: ContextIntegrity


class EvidenceAdapter(Protocol):
    source: EvidenceSource

    def fetch(
        self,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> EvidenceResult:
        ...


class RetrievalSearch(Protocol):
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        ...


SessionFactory = Callable[[], Any]


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _forbidden_if_not_allowed(
    request: EvidenceRequest,
    *,
    context: EvidenceExecutionContext,
    source: EvidenceSource,
) -> EvidenceResult | None:
    allowance = context.context_integrity.allowance_for(source)
    if allowance is None or not allowance.visible:
        return EvidenceResult(
            request_id=request.request_id,
            source=source,
            status="forbidden",
            packets=[],
            elapsed_ms=0,
        )
    forbidden_ids = [
        source_id
        for source_id in request.source_ids
        if source_id not in set(allowance.source_ids)
    ]
    if forbidden_ids:
        return EvidenceResult(
            request_id=request.request_id,
            source=source,
            status="forbidden",
            packets=[],
            elapsed_ms=0,
        )
    return None


def _result(
    request: EvidenceRequest,
    source: EvidenceSource,
    started: float,
    packets: Iterable[ContextPacket],
) -> EvidenceResult:
    packet_list = list(packets)[: request.max_items]
    return EvidenceResult(
        request_id=request.request_id,
        source=source,
        status="ok" if packet_list else "empty",
        packets=packet_list,
        elapsed_ms=_elapsed_ms(started),
    )


class _DatabaseEvidenceAdapter:
    source: EvidenceSource

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def fetch(
        self,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> EvidenceResult:
        started = perf_counter()
        forbidden = _forbidden_if_not_allowed(request, context=context, source=self.source)
        if forbidden is not None:
            return forbidden

        db = None
        try:
            db = self.session_factory()
            packets = self._fetch_packets(db, request, context=context)
            return _result(request, self.source, started, packets)
        except TimeoutError:
            return EvidenceResult(
                request_id=request.request_id,
                source=self.source,
                status="timeout",
                packets=[],
                elapsed_ms=_elapsed_ms(started),
            )
        except Exception:
            return EvidenceResult(
                request_id=request.request_id,
                source=self.source,
                status="error",
                packets=[],
                elapsed_ms=_elapsed_ms(started),
            )
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    def _fetch_packets(
        self,
        db: Any,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> list[ContextPacket]:
        raise NotImplementedError


class ConversationEvidenceAdapter(_DatabaseEvidenceAdapter):
    source = EvidenceSource.CONVERSATION

    def _fetch_packets(
        self,
        db: Any,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> list[ContextPacket]:
        session_uuid = uuid.UUID(context.context_integrity.identity.session_id)
        session = db.get(AISession, session_uuid)
        if session is None or str(getattr(session, "patient_id", "")) != context.context_integrity.identity.patient_id:
            return []

        messages = (
            db.query(AIMessage)
            .filter(AIMessage.session_id == session_uuid)
            .order_by(*ai_message_ordering())
            .all()
        )
        requested_ids = set(request.source_ids)
        packets: list[ContextPacket] = []
        for message in messages:
            source_id = f"ai_message:{message.message_id}"
            if not requested_ids:
                continue
            if source_id not in requested_ids and f"conversation:{session_uuid}" not in requested_ids:
                continue
            packets.append(
                ContextPacket(
                    source_id=source_id,
                    content=str(message.content or ""),
                    metadata={
                        "sender_role": str(message.sender_role),
                        "created_at": str(message.created_at or ""),
                        "scope": "session",
                    },
                )
            )
        return packets


class PatientMemoryEvidenceAdapter(_DatabaseEvidenceAdapter):
    source = EvidenceSource.PATIENT_MEMORY

    def _fetch_packets(
        self,
        db: Any,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> list[ContextPacket]:
        patient_uuid = uuid.UUID(context.context_integrity.identity.patient_id)
        document = db.execute(
            select(MemoryDocument).where(
                MemoryDocument.scope == "patient",
                MemoryDocument.patient_id == patient_uuid,
                MemoryDocument.care_episode_id.is_(None),
                MemoryDocument.status == "active",
            )
        ).scalars().first()
        cards = cards_from_memory_documents(document, None)
        requested_ids = set(request.source_ids)
        return [
            ContextPacket(
                source_id=card.source_id,
                content=card.text,
                metadata={**dict(card.metadata), "scope": card.scope, "source_type": card.source_type},
            )
            for card in cards
            if card.source_id in requested_ids
        ]


class CareEpisodeEvidenceAdapter(_DatabaseEvidenceAdapter):
    source = EvidenceSource.CARE_EPISODE

    def _fetch_packets(
        self,
        db: Any,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> list[ContextPacket]:
        episode_id = context.context_integrity.identity.care_episode_id
        if not episode_id:
            return []
        episode_uuid = uuid.UUID(episode_id)
        episode = db.get(CareEpisode, episode_uuid)
        if episode is None:
            return []
        if str(getattr(episode, "patient_id", "")) != context.context_integrity.identity.patient_id:
            return []
        if (getattr(episode, "status", None) or "active") != "active":
            return []

        requested_ids = set(request.source_ids)
        if not requested_ids:
            return []

        packets: list[ContextPacket] = []
        episode_source_id = f"care_episode:{episode_uuid}"
        if episode_source_id in requested_ids:
            packets.append(
                ContextPacket(
                    source_id=episode_source_id,
                    content=(
                        f"Issue: {episode.issue_title}\n"
                        f"Body area: {episode.body_area}\n"
                        f"Goal: {episode.goal}\n"
                        f"Description: {episode.short_description}"
                    ),
                    metadata={
                        "scope": "episode",
                        "care_episode_id": str(episode_uuid),
                        "patient_id": str(episode.patient_id),
                        "status": str(getattr(episode, "status", None) or "active"),
                    },
                )
            )

        document = db.execute(
            select(MemoryDocument).where(
                MemoryDocument.scope == "episode",
                MemoryDocument.patient_id == episode.patient_id,
                MemoryDocument.care_episode_id == episode.care_episode_id,
                MemoryDocument.status == "active",
            )
        ).scalars().first()
        cards = cards_from_memory_documents(None, document)
        packets.extend(
            ContextPacket(
                source_id=card.source_id,
                content=card.text,
                metadata={**dict(card.metadata), "scope": "episode", "source_type": card.source_type},
            )
            for card in cards
            if card.source_id in requested_ids
        )

        if any(source_id.startswith("rehab_activity:") for source_id in requested_ids):
            rehab_session = latest_rehab_session_for_episode(db, episode_uuid, episode.patient_id)
            packets.extend(
                ContextPacket(
                    source_id=card.source_id,
                    content=card.text,
                    metadata={**dict(card.metadata), "scope": "episode", "source_type": card.source_type},
                )
                for card in cards_from_rehab_session(rehab_session)
                if card.source_id in requested_ids
            )

        if document is not None and any(
            source_id.startswith("triage_summary:") for source_id in requested_ids
        ):
            summary = latest_triage_summary_for_episode(db, episode_uuid)
            if (
                summary is not None
                and str(getattr(summary, "care_episode_id", "")) == str(episode_uuid)
                and str(getattr(summary, "patient_id", "")) == str(episode.patient_id)
            ):
                packets.extend(
                    ContextPacket(
                        source_id=card.source_id,
                        content=card.text,
                        metadata={
                            **dict(card.metadata),
                            "scope": "episode",
                            "source_type": card.source_type,
                        },
                    )
                    for card in cards_from_triage_summary(summary)
                    if card.source_id in requested_ids
                )
        return packets


class RehabKnowledgeEvidenceAdapter:
    source = EvidenceSource.REHAB_KNOWLEDGE

    def __init__(self, retrieval: RetrievalSearch | Any) -> None:
        self.retrieval = retrieval

    def fetch(
        self,
        request: EvidenceRequest,
        *,
        context: EvidenceExecutionContext,
    ) -> EvidenceResult:
        started = perf_counter()
        forbidden = _forbidden_if_not_allowed(request, context=context, source=self.source)
        if forbidden is not None:
            return forbidden
        try:
            requested_ids = set(request.source_ids)
            allowance = context.context_integrity.allowance_for(self.source)
            authorized_ids = set(allowance.source_ids if allowance is not None else ()) & requested_ids
            dynamic_scope = bool(
                allowance is not None
                and PUBLIC_REHAB_KNOWLEDGE_SCOPE in allowance.source_ids
            )
            search = getattr(self.retrieval, "search", None)
            if not callable(search):
                search = getattr(self.retrieval, "rehab_exercise_kb_search", None)
            if not callable(search):
                raise TypeError("retrieval adapter must supply search(query, limit)")
            try:
                documents = search(query=request.query, limit=request.max_items)
            except TypeError:
                documents = search(request.query, request.max_items)
            packets = []
            for document in documents or []:
                if not isinstance(document, dict):
                    continue
                payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
                content = payload.get("text") or payload.get("content") or document.get("text") or document.get("page_content") or ""
                raw_id = payload.get("document_id") or document.get("id") or document.get("source_id")
                if raw_id is None and content:
                    raw_id = hashlib.sha256(str(content).encode("utf-8")).hexdigest()[:16]
                if raw_id is None:
                    continue
                source_id = str(raw_id).strip()
                if not source_id:
                    continue
                if not source_id.startswith(("retrieved_doc:", "knowledge_document:", "rehab_knowledge:")):
                    source_id = f"retrieved_doc:{source_id}"
                if source_id not in authorized_ids and not dynamic_scope:
                    continue
                if dynamic_scope and not context.context_integrity.source_id_is_allowed(self.source, source_id):
                    continue
                metadata = {
                    str(key): value
                    for key, value in payload.items()
                    if key not in {"text", "content", "page_content"}
                }
                packets.append(ContextPacket(source_id=source_id, content=str(content), metadata=metadata))
            return _result(request, self.source, started, packets)
        except TimeoutError:
            return EvidenceResult(
                request_id=request.request_id,
                source=self.source,
                status="timeout",
                packets=[],
                elapsed_ms=_elapsed_ms(started),
            )
        except Exception:
            return EvidenceResult(
                request_id=request.request_id,
                source=self.source,
                status="error",
                packets=[],
                elapsed_ms=_elapsed_ms(started),
            )


def build_production_adapters(
    *,
    session_factory: SessionFactory,
    retrieval: RetrievalSearch | Any,
) -> dict[EvidenceSource, EvidenceAdapter]:
    """Construct fresh-resource adapters; no adapter shares a SQLAlchemy Session."""
    def factory_for_adapter() -> SessionFactory:
        return lambda: session_factory()

    return {
        EvidenceSource.CONVERSATION: ConversationEvidenceAdapter(factory_for_adapter()),
        EvidenceSource.PATIENT_MEMORY: PatientMemoryEvidenceAdapter(factory_for_adapter()),
        EvidenceSource.CARE_EPISODE: CareEpisodeEvidenceAdapter(factory_for_adapter()),
        EvidenceSource.REHAB_KNOWLEDGE: RehabKnowledgeEvidenceAdapter(retrieval),
    }


ConversationAdapter = ConversationEvidenceAdapter
PatientMemoryAdapter = PatientMemoryEvidenceAdapter
CareEpisodeAdapter = CareEpisodeEvidenceAdapter
RehabKnowledgeAdapter = RehabKnowledgeEvidenceAdapter
