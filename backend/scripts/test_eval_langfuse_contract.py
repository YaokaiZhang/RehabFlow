"""Contract checks for the LLM-judge eval on Anthropic and its Langfuse reporting.

Runs offline: the Anthropic client is a fake and Langfuse spans go to an
in-memory OTel exporter. Run with
``python scripts/test_eval_langfuse_contract.py`` or pytest.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://rehab:rehab@localhost:5432/rehab_agent")

from evals import langfuse_reporting
from evals.langfuse_reporting import EvalLangfuseReporter, case_backend_environment, case_scores
from evals.real_model.run import _AnthropicJudgeProvider, normalize_judge_effort


class _FakeStream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._message


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    def stream(self, **request: Any) -> _FakeStream:
        self.requests.append(request)
        return _FakeStream(
            SimpleNamespace(
                model=request["model"],
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(type="thinking", thinking="private reasoning"),
                    SimpleNamespace(type="text", text=self.text),
                ],
                usage=SimpleNamespace(
                    input_tokens=1200,
                    output_tokens=340,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )
        )


def _fake_client(text: str = '{"criteria": {}}') -> Any:
    return SimpleNamespace(messages=_FakeMessages(text))


def _memory_reporter(public_key: str):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-lf-test",
        base_url="http://langfuse.invalid",
        environment=langfuse_reporting.EVAL_ENVIRONMENT,
        span_exporter=exporter,
    )
    scores: list[dict[str, Any]] = []
    client.create_score = lambda **kwargs: scores.append(kwargs)  # type: ignore[method-assign]
    return EvalLangfuseReporter(client, run_id="run-1"), exporter, scores


_REPORT = {
    "status": "completed",
    "judge_status": "failed",
    "judge": {
        "criteria": {
            "clinical_safety": {
                "score": 1,
                "passed": False,
                "reason_codes": ["missed_red_flag"],
                "justification": "Did not route the chest pain to urgent care.",
            },
            "grounding": {"score": 4, "passed": True, "reason_codes": [], "justification": None},
        }
    },
    "rubric_summary": {"passed": False, "weighted_score": 2.5, "hard_gate_failures": ["clinical_safety"]},
}


def test_judge_uses_anthropic_with_adaptive_thinking_and_effort() -> None:
    client = _fake_client('{"criteria": {"grounding": {"score": 3}}}')
    judge = _AnthropicJudgeProvider(api_key="unused", model_name="claude-opus-5-5", effort="high", client=client)
    message = judge.invoke("grade this", model="claude-opus-5-5")
    request = client.messages.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "high"}
    assert request["messages"] == [{"role": "user", "content": "grade this"}]
    assert "temperature" not in request and "budget_tokens" not in str(request)
    # Only text blocks reach the judge parser; thinking stays out.
    assert message.content == '{"criteria": {"grounding": {"score": 3}}}'
    assert message.usage_metadata == {"input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540}
    try:
        judge.invoke("x", model="other-model")
    except ValueError:
        pass
    else:
        raise AssertionError("a model mismatch must be rejected")


def test_judge_effort_validation() -> None:
    assert normalize_judge_effort("HIGH") == "high"
    assert normalize_judge_effort("none") is None
    assert normalize_judge_effort("") is None
    try:
        normalize_judge_effort("minimal")
    except ValueError:
        pass
    else:
        raise AssertionError("an OpenAI-only effort must be rejected")
    judge = _AnthropicJudgeProvider(api_key="unused", effort="none", client=_fake_client())
    judge.invoke("x", model=judge.model_name)
    assert "output_config" not in judge.client.messages.requests[0]


def test_case_backend_environment_tags_the_case() -> None:
    env = case_backend_environment("run-1", "longitudinal", "lt,07")
    assert env == {
        "LANGFUSE_TRACING_ENVIRONMENT": "evaluation",
        "LANGFUSE_TRACE_TAGS": "eval,eval-run:run-1,eval-case:longitudinal:lt_07",
    }


def test_case_scores_map_rubric_and_summary() -> None:
    scores = {score["name"]: score for score in case_scores(_REPORT)}
    assert scores["eval.judge_status"]["value"] == "failed"
    assert scores["eval.judge_status"]["data_type"] == "CATEGORICAL"
    assert scores["rubric.clinical_safety"]["value"] == 1.0
    assert scores["rubric.clinical_safety"]["comment"].startswith("Did not route")
    assert scores["rubric.clinical_safety"]["metadata"]["reason_codes"] == ["missed_red_flag"]
    assert scores["rubric.grounding"]["comment"] is None
    assert scores["rubric.weighted_score"]["value"] == 2.5
    assert scores["eval.passed"] == {
        "name": "eval.passed",
        "value": 0.0,
        "data_type": "BOOLEAN",
        "comment": "clinical_safety",
    }
    # A case the judge never saw still reports its status.
    assert [s["name"] for s in case_scores({"judge_status": "not_attempted", "failures": ["X"]})] == [
        "eval.judge_status"
    ]


def test_case_trace_holds_judge_generations_and_scores() -> None:
    reporter, exporter, scores = _memory_reporter("pk-lf-test-eval-case")
    judge = _AnthropicJudgeProvider(
        api_key="unused",
        model_name="claude-opus-5-5",
        client=_fake_client('{"criteria": {}}'),
        langfuse=reporter,
    )
    with reporter.case(
        suite="longitudinal",
        case_id="lt-07",
        session_ids={"primary": "sess-a", "follow_up": "sess-b"},
    ) as case_trace:
        judge.invoke("first attempt", model="claude-opus-5-5")
        judge.invoke("repair attempt", model="claude-opus-5-5")
        reporter.record_case_result(case_trace, _REPORT)
    reporter.flush()

    spans = exporter.get_finished_spans()
    assert len({span.context.trace_id for span in spans}) == 1, [span.name for span in spans]
    by_name: dict[str, list[Any]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    (case_span,) = by_name[langfuse_reporting.CASE_TRACE_NAME]
    generations = by_name[langfuse_reporting.JUDGE_GENERATION_NAME]
    assert len(generations) == 2
    for generation in generations:
        assert generation.parent.span_id == case_span.context.span_id
        assert generation.attributes.get("langfuse.observation.type") == "generation"
        assert generation.attributes.get("langfuse.observation.model.name") == "claude-opus-5-5"
        assert '"input": 1200' in generation.attributes.get("langfuse.observation.usage_details", "")
    for span in spans:
        assert span.attributes.get("session.id") == "sess-a"
        assert list(span.attributes.get("langfuse.trace.tags")) == [
            "eval",
            "eval-run:run-1",
            "eval-case:longitudinal:lt-07",
        ]
    exported = " ".join(str(dict(span.attributes)) for span in spans)
    assert "first attempt" in exported and "repair attempt" in exported
    assert "private reasoning" not in exported

    trace_id = format(case_span.context.trace_id, "032x")
    assert scores and all(score["trace_id"] == trace_id for score in scores)
    assert {score["name"] for score in scores} == {
        "eval.judge_status",
        "rubric.clinical_safety",
        "rubric.grounding",
        "rubric.weighted_score",
        "eval.passed",
    }


def test_reporter_is_off_without_explicit_enable() -> None:
    saved = {key: os.environ.get(key) for key in ("LANGFUSE_ENABLED", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")}
    try:
        os.environ.pop("LANGFUSE_ENABLED", None)
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk"
        assert EvalLangfuseReporter.from_environment(run_id="r") is None
        os.environ["LANGFUSE_ENABLED"] = "true"
        os.environ.pop("LANGFUSE_SECRET_KEY")
        assert EvalLangfuseReporter.from_environment(run_id="r") is None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_judge_errors_do_not_depend_on_langfuse() -> None:
    class _Broken:
        @contextmanager
        def judge_generation(self, **_kwargs: Any):
            yield langfuse_reporting._NoopObservation()

    judge = _AnthropicJudgeProvider(api_key="unused", client=_fake_client("ok"), langfuse=_Broken())
    assert judge.invoke("x", model=judge.model_name).content == "ok"


def test_child_backend_gets_anthropic_key_but_not_the_judge_key() -> None:
    from evals.combined.service import ServiceHarness, ServiceHarnessConfig

    saved = dict(os.environ)
    try:
        os.environ.update({
            "LLM_KEY": "parent-generation-key",
            "LLM_JUDGE_KEY": "judge-only-key",
            "OPENAI_API_KEY": "embedding-key",
        })
        config = ServiceHarnessConfig(
            backend_dir=Path(__file__).resolve().parents[1],
            python_bin=Path(sys.executable),
            host="127.0.0.1",
            port=0,
            environment="evaluation",
            backend_generation_key="generation-key",
            extra_environment=case_backend_environment("run-1", "real_model", "rm-1"),
        )
        environment = ServiceHarness(config)._environment()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert environment["LLM_KEY"] == "generation-key"
    assert "LLM_JUDGE_KEY" not in environment
    assert environment["OPENAI_API_KEY"] == "embedding-key"
    assert environment["LANGFUSE_TRACING_ENVIRONMENT"] == "evaluation"
    assert environment["LANGFUSE_TRACE_TAGS"] == "eval,eval-run:run-1,eval-case:real_model:rm-1"


def main() -> None:
    test_judge_uses_anthropic_with_adaptive_thinking_and_effort()
    test_judge_effort_validation()
    test_case_backend_environment_tags_the_case()
    test_case_scores_map_rubric_and_summary()
    test_case_trace_holds_judge_generations_and_scores()
    test_reporter_is_off_without_explicit_enable()
    test_judge_errors_do_not_depend_on_langfuse()
    test_child_backend_gets_anthropic_key_but_not_the_judge_key()
    print("eval langfuse contract: ok")


if __name__ == "__main__":
    main()
