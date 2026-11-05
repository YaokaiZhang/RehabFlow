"""Evaluator results in the self-hosted Langfuse.

When the evaluator runs with Langfuse enabled, it sends:

* the eval fixtures as the Langfuse dataset ``rehabflow-eval`` (override with
  ``LANGFUSE_EVAL_DATASET``): one item per case, upserted at the start of every
  run, with the fixture as the input and its oracles as the expected output.
  Items whose case left the fixtures are archived (see :meth:`sync_dataset`);
* each run as one experiment run on that dataset, named after the run id, with
  the pass rate and mean weighted score as run-level scores;
* every case backend tags its turn traces ``eval``, ``eval-run:<run id>`` and
  ``eval-case:<suite>:<case id>`` and reports them under the Langfuse
  environment ``evaluation`` (see :func:`case_backend_environment`);
* each case gets one ``eval_case`` trace with the same tags, placed in the
  case's first chat session, holding the judge calls as generations. It is the
  case's experiment item trace, so it reports under the environment Langfuse
  uses for experiments (``sdk-experiment``);
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
import uuid

from evals.trace import redact_trace_value

logger = logging.getLogger("evals.langfuse_reporting")

EVAL_ENVIRONMENT = "evaluation"
CASE_TRACE_NAME = "eval_case"
JUDGE_GENERATION_NAME = "eval_judge"
_TRUE = {"1", "true", "yes", "on"}
_COMMENT_LIMIT = 2000
DEFAULT_DATASET_NAME = "rehabflow-eval"
_DATASET_ITEM_NAMESPACE = uuid.UUID("5d1f3a52-7c0e-4f39-9b8a-2e6f0c4d7a11")


def dataset_name_from_environment() -> str:
    return os.getenv("LANGFUSE_EVAL_DATASET", "").strip() or DEFAULT_DATASET_NAME


def dataset_item_id(dataset_name: str, suite: str, case_id: str) -> str:
    """Stable item id, so every run upserts the same item for a case.

    Langfuse item ids are global across datasets, so the dataset name is part
    of the seed.
    """
    return str(uuid.uuid5(_DATASET_ITEM_NAMESPACE, f"{dataset_name}/{suite}/{case_id}"))


def real_model_dataset_item(case: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    return {
        "suite": "real_model",
        "case_id": case_id,
        "input": {
            "messages": case.get("messages"),
            "authorized_evidence": case.get("authorized_evidence"),
        },
        "expected_output": {
            "route": case.get("route"),
            "review_contract": case.get("review_contract"),
        },
        "metadata": {"suite": "real_model", "case_id": case_id},
    }


def longitudinal_dataset_item(scenario: Any) -> dict[str, Any]:
    case_id = str(scenario.scenario_id)
    return {
        "suite": "longitudinal",
        "case_id": case_id,
        "input": {
            "initial_state": scenario.initial_state,
            "timeline": list(scenario.timeline),
            "script": scenario.script,
        },
        "expected_output": {
            "oracles": scenario.oracles,
            "rubric_applicability": scenario.rubric_applicability,
        },
        "metadata": {
            "suite": "longitudinal",
            "case_id": case_id,
            "schema_version": scenario.schema_version,
        },
    }


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

    def __init__(
        self,
        client: Any,
        *,
        run_id: str,
        dataset_name: str = DEFAULT_DATASET_NAME,
        secrets: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.dataset_name = dataset_name
        self.secrets = tuple(str(item) for item in secrets if str(item or "").strip())
        self.experiment_id = _experiment_id(run_id)
        self.experiment_metadata: dict[str, Any] = {}
        self.dataset_id: str | None = None
        self._item_ids: dict[tuple[str, str], str] = {}

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
        return cls(
            client,
            run_id=run_id,
            dataset_name=dataset_name_from_environment(),
            secrets=secrets,
        )

    def sync_dataset(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        experiment_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Upsert the eval cases as dataset items and archive dropped cases.

        Items come from :func:`real_model_dataset_item` and
        :func:`longitudinal_dataset_item`. Returns whether the dataset is ready;
        when it is not, case traces are still sent, just not linked to it.
        """
        self.experiment_metadata = dict(experiment_metadata or {})
        try:
            try:
                dataset = self.client.get_dataset(self.dataset_name)
            except Exception:
                dataset = self.client.create_dataset(
                    name=self.dataset_name,
                    description="RehabFlow LLM-judge eval cases (real_model and longitudinal suites).",
                    metadata={"source": "backend/evals"},
                )
                dataset = self.client.get_dataset(self.dataset_name)
            dataset_id = str(dataset.id)
            existing = {str(item.id): item for item in getattr(dataset, "items", None) or ()}
        except Exception:
            logger.warning("langfuse_dataset_unavailable name=%s", self.dataset_name, exc_info=True)
            return False
        item_ids: dict[tuple[str, str], str] = {}
        for item in items:
            suite = str(item["suite"])
            case_id = str(item["case_id"])
            item_id = dataset_item_id(self.dataset_name, suite, case_id)
            try:
                self.client.create_dataset_item(
                    dataset_name=self.dataset_name,
                    id=item_id,
                    input=self._redact(item.get("input")),
                    expected_output=self._redact(item.get("expected_output")),
                    metadata=self._redact(item.get("metadata")),
                    status="ACTIVE",
                )
            except Exception:
                logger.warning("langfuse_dataset_item_failed case=%s:%s", suite, case_id, exc_info=True)
                continue
            item_ids[(suite, case_id)] = item_id
        current = set(item_ids.values())
        for item_id, stale in existing.items():
            status = str(getattr(stale, "status", "") or "").upper()
            if item_id in current or status.endswith("ARCHIVED"):
                continue
            try:
                # create_dataset_item upserts by id; keep the stale content.
                self.client.create_dataset_item(
                    dataset_name=self.dataset_name,
                    id=item_id,
                    input=getattr(stale, "input", None),
                    expected_output=getattr(stale, "expected_output", None),
                    metadata=getattr(stale, "metadata", None),
                    status="ARCHIVED",
                )
            except Exception:
                logger.debug("langfuse_dataset_item_archive_failed id=%s", item_id, exc_info=True)
        self.dataset_id = dataset_id
        self._item_ids = item_ids
        return True

    def _redact(self, value: Any) -> Any:
        return redact_trace_value(value, secrets=self.secrets)

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
        experiment = self._link_experiment_item(span, suite=suite, case_id=case_id)
        managers = [manager for manager in (experiment, span_manager, attributes) if manager is not None]
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:
            error = exc
            raise
        finally:
            for manager in managers:
                try:
                    if error is None:
                        manager.__exit__(None, None, None)
                    else:
                        manager.__exit__(type(error), error, error.__traceback__)
                except Exception:
                    logger.debug("langfuse_case_close_failed", exc_info=True)

    def _link_experiment_item(self, span: Any, *, suite: str, case_id: str) -> Any:
        """Make the open case trace this run's experiment item for the case.

        Mirrors what ``Langfuse.run_experiment`` does for each item: the
        experiment attributes propagate to the case span and everything opened
        under it, and the case span is the item's root observation. These
        helpers are private to the SDK, so ``langfuse`` stays pinned in
        requirements.txt; on any failure the trace is sent unlinked.
        """
        item_id = self._item_ids.get((suite, case_id))
        if not item_id or not self.dataset_id:
            return None
        try:
            from opentelemetry import context as otel_context
            from langfuse._client.attributes import _serialize
            from langfuse._client.propagation import (
                PropagatedExperimentAttributes,
                _get_propagated_attributes_from_context,
                _propagate_attributes,
            )
            from langfuse._client.span import _set_span_attributes_within_limit

            manager = _propagate_attributes(
                experiment=PropagatedExperimentAttributes(
                    experiment_id=self.experiment_id,
                    experiment_name=self.run_id,
                    experiment_metadata=_serialize(self.experiment_metadata or None),
                    experiment_dataset_id=self.dataset_id,
                    experiment_item_id=item_id,
                    experiment_item_metadata=_serialize({"suite": suite, "case_id": case_id}),
                    experiment_item_root_observation_id=span.id,
                )
            )
            manager.__enter__()
        except Exception:
            logger.debug("langfuse_experiment_link_failed", exc_info=True)
            return None
        try:
            # Propagation covers spans opened from here on; the case span is
            # already open, so copy the attributes onto it too.
            _set_span_attributes_within_limit(
                span._otel_span,
                _get_propagated_attributes_from_context(otel_context.get_current()),
            )
        except Exception:
            logger.debug("langfuse_experiment_root_backfill_failed", exc_info=True)
        return manager

    def record_run_summary(self, reports: Sequence[Mapping[str, Any]]) -> None:
        """Run-level scores on the experiment run: pass rate and mean weighted score."""
        if not self._item_ids:
            return
        passed: list[bool] = []
        weighted: list[float] = []
        for report in reports:
            summary = report.get("rubric_summary")
            if not isinstance(summary, Mapping):
                continue
            if isinstance(summary.get("passed"), bool):
                passed.append(summary["passed"])
            value = summary.get("weighted_score")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                weighted.append(float(value))
        scores = []
        if passed:
            scores.append({
                "name": "eval.pass_rate",
                "value": sum(passed) / len(passed),
                "comment": f"{sum(passed)}/{len(passed)} cases passed",
            })
        if weighted:
            scores.append({
                "name": "rubric.mean_weighted_score",
                "value": sum(weighted) / len(weighted),
            })
        for score in scores:
            try:
                self.client.create_score(
                    dataset_run_id=self.experiment_id,
                    data_type="NUMERIC",
                    **score,
                )
            except Exception:
                logger.debug("langfuse_run_score_failed name=%s", score["name"], exc_info=True)

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


def _experiment_id(run_id: str) -> str:
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=f"rehabflow-eval-run/{run_id}")


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
