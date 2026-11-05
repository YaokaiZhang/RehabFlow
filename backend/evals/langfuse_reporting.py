"""Evaluator results in the self-hosted Langfuse.

Three things land in Langfuse when the evaluator runs with Langfuse enabled:

* every case backend tags its turn traces ``eval``, ``eval-run:<run id>`` and
  ``eval-case:<suite>:<case id>`` and reports them under the Langfuse
  environment ``evaluation`` (see :func:`case_backend_environment`);
* each case gets one ``eval_case`` trace with the same tags, placed in the
  case's first chat session, holding the judge calls as generations;
* the judge's rubric scores, weighted score, pass/fail and judge status are
  attached to that trace as Langfuse scores, with the justification as the
  comment.

Langfuse is a debugging aid here: any failure (package missing, server down,
bad keys) is logged and ignored, and never changes an evaluation result.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import logging
import os
from typing import Any

from evals.trace import redact_trace_value

logger = logging.getLogger("evals.langfuse_reporting")

EVAL_ENVIRONMENT = "evaluation"
CASE_TRACE_NAME = "eval_case"
JUDGE_GENERATION_NAME = "eval_judge"
_TRUE = {"1", "true", "yes", "on"}
_COMMENT_LIMIT = 2000


def _tag_part(value: object) -> str:
    # Backend tags arrive as one comma-separated variable.
    return str(value).strip().replace(",", "_")


def case_tags(run_id: str, suite: str, case_id: str) -> tuple[str, ...]:
    return (
        "eval",
        f"eval-run:{_tag_part(run_id)}",
        f"eval-case:{_tag_part(suite)}:{_tag_part(case_id)}",
    )


def case_backend_environment(run_id: str, suite: str, case_id: str) -> dict[str, str]:
    """Environment for one case backend so its traces carry the case tags."""
    return {
        "LANGFUSE_TRACING_ENVIRONMENT": EVAL_ENVIRONMENT,
        "LANGFUSE_TRACE_TAGS": ",".join(case_tags(run_id, suite, case_id)),
    }


def primary_session_id(session_ids: Mapping[str, str] | Sequence[str] | None) -> str | None:
    """The case's first chat session (manifest order), used as the trace session."""
    if not session_ids:
        return None
    values = session_ids.values() if isinstance(session_ids, Mapping) else session_ids
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


class _NoopObservation:
    trace_id: str | None = None

    def update(self, **_kwargs: Any) -> None:
        return None


class EvalLangfuseReporter:
    """Writes evaluator case traces, judge generations and scores to Langfuse."""

    def __init__(self, client: Any, *, run_id: str) -> None:
        self.client = client
        self.run_id = run_id

    @classmethod
    def from_environment(
        cls,
        *,
        run_id: str,
        secrets: Sequence[str] = (),
    ) -> "EvalLangfuseReporter | None":
        """Build a reporter from ``LANGFUSE_*``; ``None`` when tracing is off."""
        if os.getenv("LANGFUSE_ENABLED", "").strip().lower() not in _TRUE:
            return None
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
        if not public_key or not secret_key:
            logger.warning("langfuse_disabled: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
            return None
        try:
            from langfuse import Langfuse

            known_secrets = tuple(str(item) for item in secrets if str(item or "").strip())

            def mask(*, data: Any, **_kwargs: Any) -> Any:
                try:
                    return redact_trace_value(data, secrets=known_secrets)
                except Exception:
                    return "[REDACTION_FAILED]"

            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=os.getenv("LANGFUSE_BASE_URL", "").strip() or "http://localhost:3100",
                environment=EVAL_ENVIRONMENT,
                mask=mask,
            )
        except Exception:
            logger.warning("langfuse_disabled: client initialization failed", exc_info=True)
            return None
        return cls(client, run_id=run_id)

    def backend_environment(self, suite: str, case_id: str) -> dict[str, str]:
        return case_backend_environment(self.run_id, suite, case_id)

    @contextmanager
    def case(
        self,
        *,
        suite: str,
        case_id: str,
        session_ids: Mapping[str, str] | Sequence[str] | None = None,
    ) -> Iterator[Any]:
        """Open the case trace; judge generations made inside nest under it."""
        try:
            from langfuse import propagate_attributes

            attributes = propagate_attributes(
                trace_name=CASE_TRACE_NAME,
                session_id=primary_session_id(session_ids),
                tags=list(case_tags(self.run_id, suite, case_id)),
            )
            attributes.__enter__()
        except Exception:
            logger.debug("langfuse_case_attributes_failed", exc_info=True)
            yield _NoopObservation()
            return
        try:
            span_manager = self.client.start_as_current_observation(
                name=CASE_TRACE_NAME,
                as_type="span",
                input={"suite": suite, "case_id": case_id},
                metadata={"run_id": self.run_id, "suite": suite, "case_id": case_id},
            )
            span = span_manager.__enter__()
        except Exception:
            logger.debug("langfuse_case_span_failed", exc_info=True)
            attributes.__exit__(None, None, None)
            yield _NoopObservation()
            return
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:
            error = exc
            raise
        finally:
            for manager in (span_manager, attributes):
                try:
                    if error is None:
                        manager.__exit__(None, None, None)
                    else:
                        manager.__exit__(type(error), error, error.__traceback__)
                except Exception:
                    logger.debug("langfuse_case_close_failed", exc_info=True)

    @contextmanager
    def judge_generation(
        self,
        *,
        model: str,
        prompt: str,
        model_parameters: Mapping[str, Any] | None = None,
    ) -> Iterator[Any]:
        """One judge call as a Langfuse generation (child of the open case trace)."""
        try:
            manager = self.client.start_as_current_observation(
                name=JUDGE_GENERATION_NAME,
                as_type="generation",
                model=model,
                input=prompt,
                model_parameters=dict(model_parameters or {}),
            )
            generation = manager.__enter__()
        except Exception:
            logger.debug("langfuse_judge_generation_failed", exc_info=True)
            yield _NoopObservation()
            return
        error: BaseException | None = None
        try:
            yield generation
        except BaseException as exc:
            error = exc
            try:
                generation.update(level="ERROR", status_message=type(exc).__name__)
            except Exception:
                pass
            raise
        finally:
            try:
                if error is None:
                    manager.__exit__(None, None, None)
                else:
                    manager.__exit__(type(error), error, error.__traceback__)
            except Exception:
                logger.debug("langfuse_judge_generation_close_failed", exc_info=True)

    def record_case_result(self, span: Any, report: Mapping[str, Any]) -> None:
        """Attach the case outcome and judge scores to the case trace."""
        trace_id = getattr(span, "trace_id", None)
        if not trace_id:
            return
        try:
            span.update(output=case_output(report))
        except Exception:
            logger.debug("langfuse_case_output_failed", exc_info=True)
        for score in case_scores(report):
            try:
                self.client.create_score(trace_id=trace_id, **score)
            except Exception:
                logger.debug("langfuse_score_failed name=%s", score.get("name"), exc_info=True)

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            logger.debug("langfuse_flush_failed", exc_info=True)


def case_output(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: report.get(key)
        for key in ("status", "judge_status", "judge_error", "rubric_summary", "fault_profile")
        if report.get(key) is not None
    }


def _comment(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:_COMMENT_LIMIT]
    if isinstance(value, (list, tuple)) and value:
        return ", ".join(str(item) for item in value)[:_COMMENT_LIMIT]
    return None


def case_scores(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Langfuse scores for one case report (judge criteria and summary)."""
    scores: list[dict[str, Any]] = []
    judge_status = str(report.get("judge_status") or "").strip()
    if judge_status:
        scores.append({
            "name": "eval.judge_status",
            "value": judge_status,
            "data_type": "CATEGORICAL",
            "comment": _comment(report.get("judge_error") or report.get("failures")),
        })
    judge = report.get("judge")
    criteria = judge.get("criteria") if isinstance(judge, Mapping) else None
    if isinstance(criteria, Mapping):
        for dimension, evaluation in criteria.items():
            if not isinstance(evaluation, Mapping):
                continue
            score = evaluation.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                continue
            scores.append({
                "name": f"rubric.{dimension}",
                "value": float(score),
                "data_type": "NUMERIC",
                "comment": _comment(evaluation.get("justification")),
                "metadata": {
                    "passed": evaluation.get("passed"),
                    "reason_codes": list(evaluation.get("reason_codes") or []),
                },
            })
    summary = report.get("rubric_summary")
    if isinstance(summary, Mapping):
        weighted = summary.get("weighted_score")
        if isinstance(weighted, (int, float)) and not isinstance(weighted, bool):
            scores.append({
                "name": "rubric.weighted_score",
                "value": float(weighted),
                "data_type": "NUMERIC",
            })
        passed = summary.get("passed")
        if isinstance(passed, bool):
            scores.append({
                "name": "eval.passed",
                "value": 1.0 if passed else 0.0,
                "data_type": "BOOLEAN",
                "comment": _comment(summary.get("hard_gate_failures")),
            })
    return scores
