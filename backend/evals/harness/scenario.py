from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Barrier, RLock
from typing import Any


class ScriptedCallError(RuntimeError):
    """The scripted contract was called unexpectedly or was exhausted."""


class ScriptedTimeout(TimeoutError):
    """A scripted adapter timeout."""


class ScriptedBlocked(RuntimeError):
    """A scripted tool was denied before it could return an empty result."""


def input_fingerprint(value: object) -> str:
    """Hash input shape/count metadata without retaining input content."""
    if isinstance(value, Mapping):
        shape: object = {
            "kind": "mapping",
            "keys": sorted(str(key) for key in value),
            "value_types": sorted(type(item).__name__ for item in value.values()),
        }
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        shape = {
            "kind": "sequence",
            "count": len(value),
            "item_types": [type(item).__name__ for item in value],
            "item_lengths": [len(getattr(item, "content", "")) for item in value],
        }
    else:
        shape = {"kind": type(value).__name__, "present": value is not None}
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def input_count(value: object) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return 1 if value is not None else 0


@dataclass(frozen=True)
class ScriptedLedgerEntry:
    adapter: str
    call_index: int
    input_fingerprint: str
    input_count: int


class ScriptedCallLedger:
    """Thread-safe metadata ledger; prompts and responses are never stored."""

    def __init__(self, trace_sink: object | None = None) -> None:
        self._entries: list[ScriptedLedgerEntry] = []
        self._trace_sink = trace_sink
        self._lock = RLock()

    def record(
        self,
        *,
        adapter: str,
        call_index: int,
        input_fingerprint: str,
        input_count: int = 0,
    ) -> None:
        with self._lock:
            self._entries.append(
                ScriptedLedgerEntry(
                    adapter=str(adapter),
                    call_index=int(call_index),
                    input_fingerprint=str(input_fingerprint),
                    input_count=int(input_count),
                )
            )

    def record_trace_event(self, event_type: str, payload: object) -> None:
        sink = self._trace_sink
        if sink is None:
            return
        record = getattr(sink, "record", None)
        if callable(record):
            record(str(event_type), payload)
        elif callable(sink):
            sink({"event_type": str(event_type), "payload": payload})

    def record_model_response(self, node_name: str, response: object) -> None:
        payload: dict[str, object] = {
            "node": str(node_name),
            "parsed_output": response,
        }
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, Mapping):
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and value >= 0:
                    payload[key] = value
        self.record_trace_event("model_response", payload)

    def record_model_error(
        self,
        node_name: str,
        error: BaseException,
        *,
        retryable: bool = False,
    ) -> None:
        self.record_trace_event(
            "model_error",
            {
                "node": str(node_name),
                "error_type": type(error).__name__,
                "message": str(error),
                "retryable": bool(retryable),
            },
        )
        if retryable:
            self.record_trace_event(
                "retry",
                {
                    "kind": "model",
                    "node": str(node_name),
                    "reason": type(error).__name__,
                },
            )

    def record_tool_result(self, tool_name: str, result: object) -> None:
        self.record_trace_event(
            "tool_result",
            {
                "tool_name": str(tool_name),
                "parsed_result": result,
            },
        )

    def record_tool_error(
        self,
        tool_name: str,
        error: BaseException,
        *,
        retryable: bool = False,
    ) -> None:
        self.record_trace_event(
            "tool_error",
            {
                "tool_name": str(tool_name),
                "error_type": type(error).__name__,
                "message": str(error),
                "retryable": bool(retryable),
            },
        )
        if retryable:
            self.record_trace_event(
                "retry",
                {
                    "kind": "tool",
                    "tool_name": str(tool_name),
                    "reason": type(error).__name__,
                },
            )

    @property
    def entries(self) -> tuple[ScriptedLedgerEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def normalized_entries(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "adapter": entry.adapter,
                    "call_index": entry.call_index,
                    "input_fingerprint": entry.input_fingerprint,
                    "input_count": entry.input_count,
                }
                for entry in self._entries
            )


class ScenarioScript:
    """Select exact node scripts by node name and call index."""

    def __init__(
        self,
        *,
        scenario_id: str,
        script: Mapping[str, object],
        ledger: ScriptedCallLedger,
        barriers: Mapping[str, Barrier] | None = None,
        clock: object | None = None,
    ) -> None:
        self.scenario_id = str(scenario_id)
        self._script = dict(script)
        self.ledger = ledger
        self._barriers = dict(barriers or {})
        self._clock = clock
        self._indices: dict[str, int] = {}
        self._failure: str | None = None
        self._timeout_fired = False
        self._timeout_adapters: list[str] = []
        self._control_outcomes: list[str] = []
        self._lock = RLock()

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def _mark_failure(self, message: str) -> None:
        self._failure = str(message)

    @property
    def timeout_fired(self) -> bool:
        with self._lock:
            return self._timeout_fired

    @property
    def timeout_adapters(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._timeout_adapters)

    @property
    def control_outcomes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._control_outcomes)

    def mark_control_outcome(self, outcome: str) -> None:
        with self._lock:
            value = str(outcome)
            if value not in self._control_outcomes:
                self._control_outcomes.append(value)

    def next(
        self,
        node_name: str,
        input_value: object = None,
        *,
        adapter: str | None = None,
    ) -> dict[str, Any]:
        node = str(node_name)
        with self._lock:
            script_key = node
            if script_key not in self._script and node == "rehab_exercise_kb_search" and "tools" in self._script:
                script_key = "tools"
            raw_steps = self._script.get(script_key)
            if raw_steps is None:
                message = f"unexpected scripted call node={node!r} scenario={self.scenario_id!r}"
                self._mark_failure(message)
                raise ScriptedCallError(message)
            if not isinstance(raw_steps, (list, tuple)):
                message = f"script for node={node!r} is not a call list"
                self._mark_failure(message)
                raise ScriptedCallError(message)
            call_index = self._indices.get(script_key, 0)
            if call_index >= len(raw_steps):
                message = f"exhausted scripted calls node={node!r} index={call_index}"
                self._mark_failure(message)
                raise ScriptedCallError(message)
            self._indices[script_key] = call_index + 1
            raw_step = raw_steps[call_index]

        active_adapter = adapter or node
        self.ledger.record(
            adapter=active_adapter,
            call_index=call_index,
            input_fingerprint=input_fingerprint(input_value),
            input_count=input_count(input_value),
        )
        step = {"token": raw_step} if isinstance(raw_step, str) else dict(raw_step)
        if str(active_adapter).startswith("model:"):
            self.ledger.record_trace_event(
                "model_request",
                {
                    "node": node,
                    "adapter": active_adapter,
                    "call_index": call_index,
                    "messages": input_value,
                },
            )
        elif str(active_adapter).startswith(("tool:", "evidence:")):
            self.ledger.record_trace_event(
                "tool_call",
                {
                    "tool_name": str(active_adapter).split(":", 1)[1],
                    "adapter": active_adapter,
                    "call_index": call_index,
                    "arguments": input_value,
                },
            )
        timeout = step.get("timeout") is True or step.get("outcome") == "timeout"
        if timeout:
            with self._lock:
                self._timeout_fired = True
                self._timeout_adapters.append(str(adapter or node))
        barrier_name = step.get("barrier")
        if barrier_name is not None:
            barrier = self._barriers.get(str(barrier_name))
            if barrier is None:
                raise ScriptedCallError(f"unknown scripted barrier={barrier_name!r}")
            try:
                barrier.wait(timeout=5)
            except Exception as exc:
                raise ScriptedCallError("scripted barrier failed") from exc
        elapsed_ms = step.get("elapsed_ms", step.get("latency_ms", 0))
        if timeout and not isinstance(elapsed_ms, (int, float)):
            elapsed_ms = 0
        if timeout and elapsed_ms <= 0:
            elapsed_ms = step.get("timeout_ms", 100)
        if self._clock is not None and isinstance(elapsed_ms, (int, float)) and elapsed_ms > 0:
            advance = getattr(self._clock, "advance", None)
            if callable(advance):
                advance(float(elapsed_ms) / 1000.0)
        return step
