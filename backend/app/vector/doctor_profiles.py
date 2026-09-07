from __future__ import annotations

from typing import Any

from app.db.models import CareEpisode
from app.schemas.professional_care import DoctorDirectoryEntry


MAX_CARE_EPISODE_SEARCH_TEXT_CHARS = 1200


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    return " ".join(str(value).split())


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _clean_text(value)
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned


def doctor_profile_text(doctor: DoctorDirectoryEntry) -> str:
    """Return searchable, public-safe text for a doctor directory entry."""
    sections = [
        f"Doctor: {doctor.display_name}",
        f"Specialty: {doctor.specialty or 'Rehabilitation clinician'}",
        f"Verification: {doctor.verification_status}",
        "Expertise tags: " + ", ".join(doctor.expertise_tags),
    ]
    return "\n".join(section for section in sections if not section.endswith(": "))


def _cap_search_text(text: str) -> str:
    if len(text) <= MAX_CARE_EPISODE_SEARCH_TEXT_CHARS:
        return text
    return text[: MAX_CARE_EPISODE_SEARCH_TEXT_CHARS - 3].rstrip() + "..."


def care_episode_search_text(episode: CareEpisode, query: str = "", latest_triage_summary: dict[str, Any] | None = None) -> str:
    """Return bounded search text enriched with Care Episode and Triage Summary context."""
    clean_query = _clean_text(query)
    summary = latest_triage_summary if isinstance(latest_triage_summary, dict) else {}
    sections = [
        f"Patient search: {clean_query}",
        f"Issue: {_clean_text(episode.issue_title)}",
        f"Body area: {_clean_text(episode.body_area)}",
        f"Goal: {_clean_text(episode.goal)}",
        f"Concern: {_clean_text(summary.get('concern'))}",
        "Safety signals: " + ", ".join(_clean_list(summary.get("safety_signals"))),
        f"Recommendation: {_clean_text(summary.get('recommendation'))}",
    ]
    if clean_query:
        sections.extend(
            [
                f"Description: {_clean_text(episode.short_description)}",
                f"Relevant context: {_clean_text(summary.get('relevant_context'))}",
                "Unresolved questions: " + ", ".join(_clean_list(summary.get("unresolved_questions"))),
                "Missing information: " + ", ".join(_clean_list(summary.get("missing_information"))),
            ]
        )
    return _cap_search_text("\n".join(section for section in sections if not section.endswith(": ")))
