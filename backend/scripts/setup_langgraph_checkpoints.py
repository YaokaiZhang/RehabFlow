"""Provision LangGraph Postgres checkpoint tables and verify their lifecycle.

Run this deployment command after Alembic has upgraded the application schema and
before starting the API. It writes only a fixed, non-patient smoke checkpoint and
removes it before exiting.
"""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import ContextManager

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings, get_settings

CHECKPOINTING_MODULE_PATH = BACKEND_DIR / "app" / "ai" / "checkpointing.py"
CHECKPOINTING_SPEC = importlib.util.spec_from_file_location(
    "rehabflow_checkpointing",
    CHECKPOINTING_MODULE_PATH,
)
if CHECKPOINTING_SPEC is None or CHECKPOINTING_SPEC.loader is None:
    raise RuntimeError("Unable to load the LangGraph checkpointing module")
CHECKPOINTING_MODULE = importlib.util.module_from_spec(CHECKPOINTING_SPEC)
sys.modules[CHECKPOINTING_SPEC.name] = CHECKPOINTING_MODULE
CHECKPOINTING_SPEC.loader.exec_module(CHECKPOINTING_MODULE)
open_checkpoint_saver = CHECKPOINTING_MODULE.open_checkpoint_saver

RESERVED_DEPLOYMENT_THREAD_ID = "__rehabflow_deployment_checkpoint_smoke__"
SMOKE_CONFIG = {
    "configurable": {
        "thread_id": RESERVED_DEPLOYMENT_THREAD_ID,
        "checkpoint_ns": "",
    }
}
SMOKE_PAYLOAD = "rehabflow-deployment-checkpoint-smoke"
SMOKE_CHECKPOINT = {
    "v": 2,
    "id": "00000000-0000-0000-0000-000000000005",
    "ts": "2026-07-18T00:00:00+00:00",
    "channel_values": {"deployment_smoke": SMOKE_PAYLOAD},
    "channel_versions": {},
    "versions_seen": {},
    "pending_sends": [],
    "updated_channels": ["deployment_smoke"],
}
SMOKE_METADATA = {
    "source": "deployment_smoke",
    "step": 0,
    "writes": {"deployment_smoke": SMOKE_PAYLOAD},
}


def run_deployment_smoke(
    settings: Settings,
    *,
    saver_factory: Callable[[Settings], ContextManager] = open_checkpoint_saver,
) -> None:
    """Set up vendor tables and verify a reserved checkpoint write/read/delete cycle."""
    with saver_factory(settings) as saver:
        saver.setup()
        saver.delete_thread(RESERVED_DEPLOYMENT_THREAD_ID)
        try:
            saved_config = saver.put(
                SMOKE_CONFIG,
                deepcopy(SMOKE_CHECKPOINT),
                SMOKE_METADATA,
                {},
            )
            stored = saver.get_tuple(saved_config)
            if stored is None or stored.checkpoint["channel_values"].get(
                "deployment_smoke"
            ) != SMOKE_PAYLOAD:
                raise RuntimeError("LangGraph checkpoint smoke read did not match its test payload")
        finally:
            saver.delete_thread(RESERVED_DEPLOYMENT_THREAD_ID)

        if saver.get_tuple(SMOKE_CONFIG) is not None:
            raise RuntimeError("LangGraph checkpoint smoke delete did not remove its reserved thread")


def main() -> int:
    try:
        run_deployment_smoke(get_settings())
    except Exception as exc:
        print(f"LangGraph checkpoint setup failed: {exc}", file=sys.stderr)
        return 1

    print("LangGraph checkpoint setup and reserved smoke verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
