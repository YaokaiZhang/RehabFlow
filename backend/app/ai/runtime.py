from __future__ import annotations

import asyncio
from typing import Any

from app.ai.active_turn_context import (
    begin_active_turn_tracking,
    clear_active_turn,
    create_active_turn,
    end_active_turn_tracking,
)
from app.ai.graph_dependencies import (
    adapt_runtime_dependencies,
    context_planner_for,
)
from app.ai.graph_builder import build_rehab_graph
from app.ai.checkpoint_privacy import ensure_metadata_only_checkpointer
from app.ai.runtime_dependencies import AgentRuntimeDependencies


class AgentRuntime:
    def __init__(self, deps: AgentRuntimeDependencies):
        self.deps = deps
        self.checkpointer = ensure_metadata_only_checkpointer(deps.checkpointer)
        # Keep the dependency seam truthful for tests and legacy callers that
        # inspect the compiled checkpointer.
        object.__setattr__(deps, "checkpointer", self.checkpointer)
        self._idempotent_results: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._idempotency_request_hashes: dict[tuple[str, str], str] = {}
        self.graph_deps = adapt_runtime_dependencies(deps)
        self.context_planner = context_planner_for(self.graph_deps)
        self.graph = build_rehab_graph(self.graph_deps).compile(checkpointer=self.checkpointer)

    @staticmethod
    def _idempotency_key(command: dict[str, Any] | Any, session_id: str) -> tuple[str, str, str] | None:
        if not isinstance(command, dict):
            return None
        key = command.get("idempotency_key")
        request_hash = command.get("request_hash")
        prior_hash = command.get("prior_request_hash")
        if not key or not request_hash or prior_hash is None:
            return None
        return (str(session_id), str(key), str(request_hash))

    def _replay_if_completed(
        self,
        command: dict[str, Any] | Any,
        *,
        session_id: str,
    ) -> tuple[tuple[str, str, str] | None, dict[str, Any] | None]:
        key = self._idempotency_key(command, session_id)
        if key is None:
            return None, None
        identity = key[:2]
        previous_hash = self._idempotency_request_hashes.get(identity)
        if previous_hash is not None and previous_hash != key[2]:
            raise ValueError("Idempotency key was already used with a different request.")
        self._idempotency_request_hashes[identity] = key[2]
        previous = self._idempotent_results.get(key)
        if previous is None:
            return key, None
        replay = dict(previous)
        replay["_idempotency_replayed"] = True
        return key, replay

    def _remember_completed(self, key: tuple[str, str, str] | None, result: dict[str, Any]) -> None:
        if key is not None and result.get("final_response") is not None:
            self._idempotent_results[key] = dict(result)

    @staticmethod
    def _owner_from_command(command: dict[str, Any] | Any) -> str | None:
        if not isinstance(command, dict):
            return None
        value = command.get("authenticated_owner") or command.get("principal_id") or command.get("patient_id")
        return str(value) if value else None

    @classmethod
    def _config_for(
        cls,
        command: dict[str, Any] | Any,
        *,
        session_id: str,
        owner_id: str | None,
    ) -> dict[str, dict[str, str]]:
        owner = owner_id or cls._owner_from_command(command)
        configurable = {"thread_id": str(session_id)}
        if owner:
            configurable["authenticated_owner"] = str(owner)
        return {"configurable": configurable}

    async def ainvoke_turn(
        self,
        command: dict[str, Any] | Any,
        *,
        session_id: str,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._config_for(command, session_id=session_id, owner_id=owner_id)
        active_turn_ids, active_turn_tracking_token = begin_active_turn_tracking()
        active_command = self._prepare_command(command, active_turn_ids, session_id=session_id)
        idempotency_key, replay = self._replay_if_completed(active_command, session_id=session_id)
        if replay is not None:
            for turn_id in active_turn_ids:
                clear_active_turn(turn_id)
            end_active_turn_tracking(active_turn_tracking_token)
            return replay
        result: dict[str, Any]
        ainvoke = getattr(self.graph, "ainvoke", None)
        try:
            with self.checkpointer.rehydrate():
                if callable(ainvoke):
                    result = await ainvoke(active_command, config=config)
                else:
                    # Narrow legacy test doubles may expose only invoke. Production
                    # LangGraph graphs always take the async path above.
                    result = await asyncio.to_thread(
                        self.graph.invoke, active_command, config=config
                    )
            self._remember_completed(idempotency_key, result)
            return result
        finally:
            if "result" in locals() and isinstance(result, dict):
                result_turn_id = result.get("evidence_turn_id")
                if result_turn_id:
                    active_turn_ids.add(str(result_turn_id))
            for turn_id in active_turn_ids:
                clear_active_turn(turn_id)
            end_active_turn_tracking(active_turn_tracking_token)

    @staticmethod
    def _prepare_command(command: dict[str, Any] | Any, active_turn_ids: set[str], session_id: str | None = None) -> Any:
        active_command = command
        if isinstance(command, dict):
            evidence_turn_id = str(command.get("evidence_turn_id") or create_active_turn(session_id))
            active_command = {
                **command,
                "evidence_turn_id": evidence_turn_id,
                "trace_turn_id": str(command.get("trace_turn_id") or evidence_turn_id),
            }
            active_turn_ids.add(evidence_turn_id)
        return active_command

    def invoke_turn_sync(
        self,
        command: dict[str, Any] | Any,
        *,
        session_id: str,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a sync LangGraph turn for interrupt-compatible checkpoint tests."""
        config = self._config_for(command, session_id=session_id, owner_id=owner_id)
        active_turn_ids, active_turn_tracking_token = begin_active_turn_tracking()
        active_command = self._prepare_command(command, active_turn_ids, session_id=session_id)
        idempotency_key, replay = self._replay_if_completed(active_command, session_id=session_id)
        if replay is not None:
            for turn_id in active_turn_ids:
                clear_active_turn(turn_id)
            end_active_turn_tracking(active_turn_tracking_token)
            return replay
        result: dict[str, Any]
        try:
            with self.checkpointer.rehydrate():
                result = self.graph.invoke(active_command, config=config)
            self._remember_completed(idempotency_key, result)
            return result
        finally:
            if "result" in locals() and isinstance(result, dict):
                result_turn_id = result.get("evidence_turn_id")
                if result_turn_id:
                    active_turn_ids.add(str(result_turn_id))
            for turn_id in active_turn_ids:
                clear_active_turn(turn_id)
            end_active_turn_tracking(active_turn_tracking_token)

    def invoke_turn(
        self,
        command: dict[str, Any] | Any,
        *,
        session_id: str,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.invoke_turn_sync(command, session_id=session_id, owner_id=owner_id)
        raise RuntimeError("invoke_turn cannot run inside an event loop; await ainvoke_turn")

    def close(self) -> None:
        close = getattr(self.deps.retrieval, "close", None)
        if callable(close):
            close()

    def delete_thread(self, session_id: str) -> None:
        self.checkpointer.delete_thread(session_id)
