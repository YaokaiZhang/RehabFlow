"""LLM check that a compacted session block still carries a critical fact.

Phrase matching cannot tell a faithful paraphrase ("no red flags") from a
dropped fact, so the evaluator asks the configured judge model to compare the
seeded critical fact with the rolling block the backend actually persisted.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any

from evals.longitudinal.judge import _decoded_provider_value


COMPACTION_JUDGE_VERSION = "v1"
COMPACTION_JUDGE_MAX_ATTEMPTS = 2

COMPACTION_JUDGE_TASK = (
    "You audit a rehabilitation chat system's session compaction. An older part of a long "
    "patient conversation was replaced by a compacted rolling block. Decide whether the "
    "critical fact below survived in the rolling block. The fact survives when every "
    "clinically material element of it can be recovered from the block with the same meaning: "
    "what is allowed or restricted, by whom, and any statement about the presence or absence "
    "of warning signs. A faithful paraphrase or condensed wording survives. The fact does not "
    "survive if any material element is missing, weakened, made more permissive or more "
    "restrictive, attributed to the wrong person, contradicted elsewhere in the block, or only "
    "recoverable by guessing. Ignore everything else in the block. Return JSON only."
)

COMPACTION_JUDGE_OUTPUT_SCHEMA = {
    "preserved": "boolean",
    "missing_or_altered": ["material element of the fact that is missing or changed"],
    "block_quote": "shortest verbatim span of the block that carries the fact, or empty string",
    "justification": "one or two sentences, at most 600 characters",
}


@dataclass(frozen=True)
class CompactionFactVerdict:
    preserved: bool | None
    missing_or_altered: tuple[str, ...]
    block_quote: str
    justification: str
    error: str | None
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_version": COMPACTION_JUDGE_VERSION,
            "preserved": self.preserved,
            "missing_or_altered": list(self.missing_or_altered),
            "block_quote": self.block_quote,
            "justification": self.justification,
            "error": self.error,
            "attempts": self.attempts,
        }


def build_compaction_fact_payload(*, critical_fact: str, rolling_block: str) -> str:
    return json.dumps(
        {
            "judge_version": COMPACTION_JUDGE_VERSION,
            "task": COMPACTION_JUDGE_TASK,
            "critical_fact": critical_fact,
            "rolling_block": rolling_block,
            "output_schema": COMPACTION_JUDGE_OUTPUT_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _parse_verdict(raw: object, rolling_block: str) -> CompactionFactVerdict:
    value = _decoded_provider_value(raw)
    if not isinstance(value, Mapping):
        raise ValueError("compaction judge did not return a JSON object")
    preserved = value.get("preserved")
    if not isinstance(preserved, bool):
        raise ValueError("compaction judge output needs a boolean 'preserved'")
    missing = value.get("missing_or_altered", [])
    if not isinstance(missing, (list, tuple)):
        raise ValueError("'missing_or_altered' must be a list")
    quote = str(value.get("block_quote") or "").strip()
    if preserved and quote and " ".join(quote.split()) not in " ".join(rolling_block.split()):
        # A "preserved" verdict must point at text that is really in the block.
        raise ValueError("'block_quote' is not a verbatim span of the rolling block")
    return CompactionFactVerdict(
        preserved=preserved,
        missing_or_altered=tuple(str(item) for item in missing if str(item).strip()),
        block_quote=quote[:600],
        justification=str(value.get("justification") or "")[:600],
        error=None,
        attempts=0,
    )


def judge_compaction_fact(
    *,
    critical_fact: str,
    rolling_block: str,
    invoke: Callable[[str], object],
) -> CompactionFactVerdict:
    payload = build_compaction_fact_payload(critical_fact=critical_fact, rolling_block=rolling_block)
    last_error = "compaction judge was not called"
    for attempt in range(1, COMPACTION_JUDGE_MAX_ATTEMPTS + 1):
        request = payload if attempt == 1 else json.dumps(
            {**json.loads(payload), "previous_validation_error": last_error},
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            verdict = _parse_verdict(invoke(request), rolling_block)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:600]
            continue
        return CompactionFactVerdict(
            preserved=verdict.preserved,
            missing_or_altered=verdict.missing_or_altered,
            block_quote=verdict.block_quote,
            justification=verdict.justification,
            error=None,
            attempts=attempt,
        )
    return CompactionFactVerdict(
        preserved=None,
        missing_or_altered=(),
        block_quote="",
        justification="",
        error=last_error,
        attempts=COMPACTION_JUDGE_MAX_ATTEMPTS,
    )
