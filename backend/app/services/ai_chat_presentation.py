"""Patient-facing presentation fields derived from a finished AI chat turn.

``next_step`` tells the client whether the turn ended in an urgent red-flag
answer (``"urgent"``), a reviewed answer it can act on (``"ready"``), or
anything else (``None``: clarification, fallback, failure).

``exercise_cards`` are built only from catalog-validated suggestions carried in
graph state, only on ``"ready"`` turns, and only for exercises the released
answer actually names, so the cards always match the prose.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

NextStep = Literal["ready", "urgent"]

NEXT_STEP_READY: NextStep = "ready"
NEXT_STEP_URGENT: NextStep = "urgent"
_NEXT_STEP_VALUES = frozenset({NEXT_STEP_READY, NEXT_STEP_URGENT})

# Router routing_decision values (app/ai/workflow_state.py, app/ai/router.py).
ROUTING_URGENT = "urgent_end"
ROUTING_CONTINUATION = "continuation"

MAX_EXERCISE_CARDS = 3
MAX_CARD_TARGETS = 3
MAX_CARD_SUMMARY_CHARS = 160

_DASHES = re.compile(r"[‐-―−]")
_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"'}


def _normalize_for_match(value: str) -> str:
    text = _DASHES.sub("-", value)
    for raw, replacement in _QUOTES.items():
        text = text.replace(raw, replacement)
    return " ".join(text.split()).casefold()


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def trim_summary(value: object, limit: int = MAX_CARD_SUMMARY_CHARS) -> str:
    """Trim to at most ``limit`` characters on a word boundary, ellipsis included."""
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def _https_url(value: object) -> str:
    url = str(value or "").strip()
    return url[:2048] if url.startswith("https://") and len(url) > len("https://") else ""


def next_step_for_turn(result: Mapping[str, Any]) -> NextStep | None:
    status = result.get("status", "completed")
    if status in {"failed", "clarification_required"}:
        return None
    final_response = _clean_text(result.get("final_response"))
    if not final_response:
        return None
    routing_decision = result.get("routing_decision")
    if routing_decision == ROUTING_URGENT or result.get("if_emergency") is True:
        return NEXT_STEP_URGENT
    if routing_decision != ROUTING_CONTINUATION:
        return None
    if result.get("needs_more_info"):
        return None
    if result.get("safety_passed") is False:
        return None
    # Both reviewers approved and the released text is the reviewed draft. An
    # unsafe/unsupported fallback replaces final_response with fallback copy, so
    # the equality check also rules those turns out.
    if result.get("review_release") is not True:
        return None
    if _clean_text(result.get("consult_response")) != final_response:
        return None
    return NEXT_STEP_READY


def exercise_card_from_suggestion(item: Mapping[str, Any]) -> dict[str, Any] | None:
    exercise_id = _clean_text(item.get("exercise_id"))
    title = _clean_text(item.get("title"))
    if not exercise_id or not title:
        return None
    raw_targets = item.get("structures_involved", item.get("targets"))
    targets: list[str] = []
    if isinstance(raw_targets, (list, tuple)):
        for raw in raw_targets:
            target = _clean_text(raw)[:80]
            if target and target not in targets:
                targets.append(target)
            if len(targets) >= MAX_CARD_TARGETS:
                break
    return {
        "exercise_id": exercise_id[:128],
        "title": title[:200],
        "summary": trim_summary(item.get("introduction", item.get("summary"))),
        "targets": targets,
        "video_url": _https_url(item.get("video_url")) or None,
        "source_url": _https_url(item.get("source_url")),
    }


def exercise_cards_for_turn(
    result: Mapping[str, Any],
    next_step: str | None,
) -> list[dict[str, Any]]:
    if next_step != NEXT_STEP_READY:
        return []
    suggestions = result.get("catalog_exercise_suggestions")
    if not isinstance(suggestions, (list, tuple)):
        return []
    prose = _normalize_for_match(str(result.get("final_response") or ""))
    if not prose:
        return []

    matches: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for rank, item in enumerate(suggestions):
        if not isinstance(item, Mapping):
            continue
        card = exercise_card_from_suggestion(item)
        if card is None or card["exercise_id"] in seen:
            continue
        title = _normalize_for_match(card["title"])
        # Leading word boundary only, so "Glute Bridge" also matches "glute bridges".
        found = re.search(r"(?<![\w])" + re.escape(title), prose)
        if found is None:
            continue
        seen.add(card["exercise_id"])
        matches.append((found.start(), rank, card))
    matches.sort(key=lambda entry: (entry[0], entry[1]))
    return [card for _, _, card in matches[:MAX_EXERCISE_CARDS]]


def chat_turn_presentation(result: Mapping[str, Any]) -> dict[str, Any]:
    next_step = next_step_for_turn(result)
    return {
        "next_step": next_step,
        "exercise_cards": exercise_cards_for_turn(result, next_step),
    }


def safe_next_step(value: object) -> NextStep | None:
    return value if value in _NEXT_STEP_VALUES else None  # type: ignore[return-value]


def safe_exercise_cards(value: object) -> list[dict[str, Any]]:
    """Re-validate stored/replayed cards before returning them to a client."""
    if not isinstance(value, (list, tuple)):
        return []
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        card = exercise_card_from_suggestion(raw)
        if card is None or card["exercise_id"] in seen:
            continue
        seen.add(card["exercise_id"])
        cards.append(card)
        if len(cards) >= MAX_EXERCISE_CARDS:
            break
    return cards


def presentation_from_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Use fields already attached by the turn service, else derive them."""
    if "next_step" in result or "exercise_cards" in result:
        next_step = safe_next_step(result.get("next_step"))
        cards = safe_exercise_cards(result.get("exercise_cards")) if next_step == NEXT_STEP_READY else []
        return {"next_step": next_step, "exercise_cards": cards}
    return chat_turn_presentation(result)
