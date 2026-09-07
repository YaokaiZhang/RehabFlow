from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.api.streaming import get_stream_ticket_service
from app.core.config import get_settings
from app.core.security import Principal, get_current_principal
from app.db.models import AISession, CareEpisode, Patient
from app.db.session import get_db
from app.schemas.ai_chat import AIChatSessionRequest, AIChatTurnRequest
from app.schemas.streaming import StreamScope
from app.services.ai_chat_access import AuthorizedAIChat, authorize_ai_chat
from app.services.dtw_service import score_motion
from app.services.stream_tickets import StreamTicketError

router = APIRouter(tags=["streaming"])
settings = get_settings()
logger = logging.getLogger(__name__)
DEBUG_GRAPH = bool(settings.debug)

_PRIVACY_SAFE_SOURCE_IDS = frozenset({"response", "rehab_knowledge:public"})
_PRIVACY_SOURCE_PREFIXES = frozenset(
    {
        "conversation",
        "episode",
        "care_episode",
        "memory_item",
        "memory_document",
        "patient_memory",
        "ai_message",
        "triage_summary",
        "rehab_activity",
        "retrieved_doc",
        "knowledge_document",
        "rehab_knowledge",
    }
)


def _privacy_safe_source_id(value: object) -> str:
    source_id = str(value or "").strip()
    if not source_id:
        return ""
    if source_id in _PRIVACY_SAFE_SOURCE_IDS:
        return source_id
    prefix, separator, physical_id = source_id.partition(":")
    if separator and prefix in _PRIVACY_SOURCE_PREFIXES and physical_id:
        handle = hashlib.sha256(physical_id.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}:{handle}"
    return f"source:{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:24]}"


def _safe_evaluation_telemetry(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    safe = {
        key: value[key]
        for key in (
            "model_call_attempts",
            "tool_call_attempts",
            "total_tokens",
            "repair_attempts",
            "elapsed_ms",
            "budget",
            "evidence_fanout",
            "execution_failures",
            "reviewer_outcomes",
        )
        if key in value
    }
    for key in ("selected_source_ids", "evidence_source_ids"):
        if key in value:
            raw_ids = value[key]
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            safe[key] = [
                _privacy_safe_source_id(item)
                for item in raw_ids
                if isinstance(item, (str, int))
            ] if isinstance(raw_ids, (list, tuple, set, frozenset)) else []
    raw_calls = value.get("tool_calls", value.get("tool_events", []))
    if isinstance(raw_calls, (list, tuple)):
        safe["tool_calls"] = [
            {
                key: item[key]
                for key in (
                    "call_id",
                    "tool_name",
                    "attempt",
                    "outcome",
                    "latency_ms",
                    "result_count",
                    "failure_category",
                    "retry_count",
                )
                if key in item
            }
            for item in raw_calls
            if isinstance(item, Mapping)
        ]
    return _json_safe_response(safe)

AI_CHAT_STREAM_ERROR_DETAIL = "AI chat temporarily unavailable."
PATIENT_STREAM_LOOP_ERROR_DETAIL = "Movement stream temporarily unavailable."
DOCTOR_STREAM_LOOP_ERROR_DETAIL = "Movement monitor temporarily unavailable."


def _json_safe_response(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe_response(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_response(item) for item in value]

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _json_safe_response(tolist())
        except (TypeError, ValueError):
            pass

    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe_response(item())
        except (TypeError, ValueError):
            pass

    return str(value)


def _safe_debug_trace(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    allowed_keys = {"step", "timestamp", "node", "attempt", "route", "routing_decision", "failure_category", "model_call", "status", "elapsed_ms", "input_tokens", "output_tokens", "total_tokens", "model_call_attempts", "tool_call_attempts", "repair_attempts", "grounding_passed", "safety_passed"}
    safe_events = []
    for raw_event in value:
        if not isinstance(raw_event, Mapping):
            continue
        event = {}
        for raw_key, raw_value in raw_event.items():
            key = str(raw_key)
            if key not in allowed_keys:
                continue
            if isinstance(raw_value, (bool, int)):
                event[key] = raw_value
            elif isinstance(raw_value, str) and len(raw_value) <= 128 and all(char.isalnum() or char in ".:_/-+" for char in raw_value):
                event[key] = raw_value
        packet_tokens = raw_event.get("context_packet_tokens")
        if isinstance(packet_tokens, Mapping):
            event["context_packet_tokens"] = {str(node): tokens for node, tokens in packet_tokens.items() if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0}
        safe_events.append(event)
    return safe_events


def _safe_chat_sources(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        parsed = urlparse(url)
        if (
            not label
            or not title
            or parsed.scheme != "https"
            or not parsed.netloc
            or url in seen_urls
        ):
            continue
        seen_urls.add(url)
        sources.append({"label": label[:80], "title": title[:240], "url": url[:2048]})
    return sources


def _build_ai_chat_response(current_session_id: str | None, result: dict, include_debug: bool) -> dict:
    response = {
        "event": "response",
        "session_id": current_session_id,
        "status": result.get("status", "completed"),
        "response": result.get("final_response", "No response generated"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": _safe_chat_sources(result.get("web_sources", result.get("sources", []))),
    }
    response["routing_decision"] = result.get("routing_decision")
    response["needs_more_info"] = result.get("needs_more_info")
    response["safety_passed"] = result.get("safety_passed")
    originating_event_id = result.get("evidence_turn_id") or result.get("trace_turn_id")
    if originating_event_id:
        response["originating_event_id"] = str(originating_event_id)

    if include_debug:
        response["debug_trace"] = _safe_debug_trace(result.get("debug_trace", []))
        response["tool_calling_used"] = result.get("tool_calling_used")
        response["tool_calling_error"] = result.get("tool_calling_error", "")
        retrieved_docs = result.get("retrieved_docs", [])
        response["retrieved_doc_count"] = (
            len(retrieved_docs) if isinstance(retrieved_docs, (list, tuple)) else 0
        )
        response["evaluation_telemetry"] = _safe_evaluation_telemetry(
            result.get("evaluation_telemetry", {})
        )

    return _json_safe_response(response)


def run_rehab_graph(
    user_input: str,
    session_id: str | None = None,
    patient_id: str | None = None,
    care_episode_id: str | None = None,
    *,
    principal_id: str | None = None,
) -> dict:
    # Keep this lightweight proxy patchable in websocket tests while deferring
    # LangGraph/Qwen imports until the AI chat endpoint actually needs them.
    from app.ai.graph_execution import run_rehab_graph as _run_rehab_graph

    return _run_rehab_graph(
        user_input,
        session_id,
        patient_id,
        care_episode_id=care_episode_id,
        principal_id=principal_id,
    )


async def ai_chat_stream(websocket: WebSocket, session_id: str) -> None:
    """Compatibility helper for the maintained offline AI chat socket contract."""
    await websocket.accept()
    current_session_id = None if session_id == "new" else session_id
    try:
        while True:
            incoming = await websocket.receive_json()
            user_input = str(incoming.get("message") or "").strip()
            include_debug = DEBUG_GRAPH
            if not user_input:
                continue
            patient_id = incoming.get("patient_id") if current_session_id is None else None
            result = await asyncio.to_thread(
                run_rehab_graph,
                user_input,
                current_session_id,
                patient_id,
            )
            current_session_id = result.get("session_id", current_session_id)
            await websocket.send_json(
                _build_ai_chat_response(
                    current_session_id,
                    result,
                    include_debug,
                )
            )
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.error("[ws:ai_chat:error] failure_category=runtime_error")
        await websocket.send_json({"event": "error", "detail": AI_CHAT_STREAM_ERROR_DETAIL})


async def _reject_stream(websocket: WebSocket) -> None:
    await websocket.close(code=1008)


@router.websocket("/rehab/stream")
async def patient_rehab_stream(websocket: WebSocket) -> None:
    ticket = websocket.query_params.get("ticket", "")
    try:
        claims = await get_stream_ticket_service().consume(ticket, StreamScope.PATIENT_REHAB)
    except StreamTicketError:
        await _reject_stream(websocket)
        return

    if claims.scope is not StreamScope.PATIENT_REHAB:
        await _reject_stream(websocket)
        return

    patient_id = str(claims.patient_id)
    await websocket.accept()

    redis = None
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        channel = f"patient_stream_{patient_id}"

        while True:
            try:
                incoming = await websocket.receive_json()
                coordinates = incoming.get("coordinates", [])
                current_score = score_motion(coordinates)

                payload = {
                    "patient_id": patient_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "coordinates": coordinates,
                    "current_score": current_score,
                }

                await redis.publish(channel, json.dumps(payload))
                await websocket.send_json({"event": "score", **payload})

            except WebSocketDisconnect:
                break
            except Exception:
                logger.warning("[ws:patient_stream] failure_category=stream_loop_failed")
                await websocket.send_json(
                    {"event": "error", "detail": PATIENT_STREAM_LOOP_ERROR_DETAIL}
                )

    except Exception:
        logger.warning("[ws:patient_stream] failure_category=stream_failed")
        await websocket.send_json({"event": "error", "detail": "Service temporarily unavailable."})
        await websocket.close(code=1011)
    finally:
        if redis:
            await redis.close()


@router.websocket("/doctor/monitor/episodes/{care_episode_id}")
async def doctor_monitor_stream(websocket: WebSocket, care_episode_id: str) -> None:
    ticket = websocket.query_params.get("ticket", "")
    try:
        claims = await get_stream_ticket_service().consume(ticket, StreamScope.DOCTOR_EPISODE_MONITOR)
        path_episode_id = UUID(care_episode_id)
    except (StreamTicketError, ValueError):
        await _reject_stream(websocket)
        return

    if claims.scope is not StreamScope.DOCTOR_EPISODE_MONITOR:
        await _reject_stream(websocket)
        return

    if claims.care_episode_id != path_episode_id:
        await _reject_stream(websocket)
        return

    patient_id = str(claims.patient_id)
    await websocket.accept()

    redis = None
    pubsub = None
    channel = f"patient_stream_{patient_id}"
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)

        await websocket.send_json({"event": "subscribed", "care_episode_id": str(path_episode_id)})

        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, str):
                        await websocket.send_text(data)
                await asyncio.sleep(0.01)
            except WebSocketDisconnect:
                break
            except Exception:
                logger.warning("[ws:doctor_monitor] failure_category=monitor_loop_failed")
                await websocket.send_json(
                    {"event": "error", "detail": DOCTOR_STREAM_LOOP_ERROR_DETAIL}
                )

    except Exception:
        logger.warning("[ws:doctor_monitor] failure_category=monitor_failed")
        await websocket.send_json({"event": "error", "detail": "Service temporarily unavailable."})
    finally:
        if pubsub:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        if redis:
            await redis.close()


_AI_CHAT_NOT_FOUND_DETAIL = "AI chat resource not found"
_AI_CHAT_RUNTIME_UNAVAILABLE_DETAIL = "AI chat runtime is unavailable"

class _LegacyAIChatTurnService:
    """Compatibility adapter for callers without the lifespan checkpointer.

    It maps an initial clarification interrupt to the HTTP response contract,
    but cannot durably resume it. The application lifespan path remains the
    supported path for checkpoint-backed clarification resume.
    """

    def invoke_turn(
        self,
        user_input: str,
        access: AuthorizedAIChat,
        *,
        idempotency_key: str | None = None,
        workflow_version: str | None = None,
    ) -> dict:
        result = run_rehab_graph(
            user_input,
            access.session_id,
            str(access.patient_id),
            care_episode_id=(
                str(access.care_episode_id) if access.care_episode_id is not None else None
            ),
            principal_id=str(access.patient_id),
        )
        if not isinstance(result, dict):
            return result
        for pending in result.get("__interrupt__") or ():
            value = getattr(pending, "value", pending)
            if not isinstance(value, Mapping) or value.get("kind") != "clarification":
                continue
            mapped = dict(result)
            mapped.pop("__interrupt__", None)
            mapped["status"] = "clarification_required"
            mapped["clarification_question"] = str(value.get("question") or "")
            mapped["final_response"] = mapped["clarification_question"]
            mapped.setdefault("session_id", access.session_id)
            return mapped
        return result



def _authorize_new_ai_chat(
    db: Session,
    principal: Principal,
    *,
    care_episode_id,
) -> AuthorizedAIChat:
    if principal.role != "patient":
        raise HTTPException(status_code=403, detail="Only patients can use AI chat")

    patient = db.query(Patient).filter(Patient.patient_id == principal.user_id).first()
    if patient is None:
        raise HTTPException(status_code=404, detail=_AI_CHAT_NOT_FOUND_DETAIL)

    episode = None
    if care_episode_id is not None:
        episode = (
            db.query(CareEpisode)
            .filter(
                CareEpisode.care_episode_id == care_episode_id,
                CareEpisode.patient_id == patient.patient_id,
            )
            .first()
        )
        if episode is None:
            raise HTTPException(status_code=404, detail=_AI_CHAT_NOT_FOUND_DETAIL)

    session = AISession(
        patient_id=patient.patient_id,
        care_episode_id=episode.care_episode_id if episode is not None else None,
    )
    db.add(session)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("[db:error] failure_category=chat_session_persistence_failed")
        raise HTTPException(status_code=500, detail="Failed to start rehab chat session") from exc

    return AuthorizedAIChat(
        patient_id=patient.patient_id,
        session_id=str(session.session_id),
        care_episode_id=episode.care_episode_id if episode is not None else None,
    )


@router.post("/ai/chat/session")
def ai_chat_new(
    request: AIChatSessionRequest,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> dict:
    access = _authorize_new_ai_chat(
        db,
        principal,
        care_episode_id=request.care_episode_id,
    )
    return {
        "event": "session_created",
        "session_id": access.session_id,
        "care_episode_id": access.care_episode_id,
    }


@router.post("/ai/chat/{session_id}")
async def ai_chat_turn(
    session_id: str,
    request: AIChatTurnRequest,
    http_request: Request = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> dict:
    user_input = request.message.strip()
    if not user_input:
        raise HTTPException(status_code=400, detail="Message is required.")

    if session_id == "new":
        access = _authorize_new_ai_chat(
            db,
            principal,
            care_episode_id=request.care_episode_id,
        )
    else:
        access = authorize_ai_chat(
            db,
            principal,
            session_id=session_id,
            care_episode_id=request.care_episode_id,
        )

    current_session_id = access.session_id
    include_debug = DEBUG_GRAPH

    if include_debug:
        logger.info(
            "[http:ai_chat] session_id=%s message_length=%s",
            current_session_id,
            len(user_input),
        )

    try:
        app_state = getattr(getattr(http_request, "app", None), "state", None)
        turn_service = getattr(app_state, "ai_chat_turn_service", None)
        if turn_service is None:
            raise HTTPException(status_code=503, detail=_AI_CHAT_RUNTIME_UNAVAILABLE_DETAIL)
        result = await asyncio.to_thread(
            turn_service.invoke_turn,
            user_input,
            access,
            idempotency_key=request.idempotency_key,
            workflow_version=request.workflow_version,
        )

        if include_debug:
            logger.info("[http:ai_chat] graph_result_keys=%s", list(result.keys()))
            logger.info("[http:ai_chat] debug_trace=%s", result.get("debug_trace", []))

        return _build_ai_chat_response(current_session_id, result, include_debug)
    except HTTPException:
        raise
    except Exception:
        logger.error("[http:ai_chat:error] failure_category=runtime_error")
        raise HTTPException(status_code=500, detail="AI chat request failed.") from None
