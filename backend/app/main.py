from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.ai.checkpointing import assert_checkpoint_tables_ready, ensure_metadata_only_checkpointer, open_checkpoint_saver
from app.ai.graph_execution import default_runtime_dependencies
from app.ai.runtime import AgentRuntime
from app.ai.workflow_state import WORKFLOW_VERSION
from app.api.auth import router as auth_router
from app.api.bindings import router as bindings_router
from app.api.care_episodes import router as care_episodes_router
from app.api.demo import router as demo_router
from app.api.episode_workspace import router as episode_workspace_router
from app.api.exercise_catalog import router as exercise_catalog_router
from app.api.diagnostics import router as diagnostics_router
from app.api.doctor_dashboard import router as doctor_dashboard_router
from app.api.doctor_profile import router as doctor_profile_router
from app.api.professional_care import router as professional_care_router
from app.api.memory_documents import router as memory_documents_router
from app.api.streaming import router as streaming_router
from app.api.ws import router as ws_router
from app.core.config import backend_source_revision, evaluator_mode_enabled, get_settings
from app.db.session import SessionLocal, verify_schema_revision
from app.services.ai_chat_turns import AIChatTurnService
from app.services.memory_maintenance_worker import MemoryMaintenanceWorker
from app.vector.qdrant_store import close_shared_qdrant_clients

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate all deployment prerequisites before opening database/checkpoint
    # resources. The validator never includes credential values in errors.
    settings.validate_runtime_configuration()
    verify_schema_revision()
    with open_checkpoint_saver(settings) as raw_saver:
        saver = raw_saver
        assert_checkpoint_tables_ready(saver)
        saver = ensure_metadata_only_checkpointer(saver)
        runtime = AgentRuntime(default_runtime_dependencies(saver))
        app.state.agent_runtime = runtime
        app.state.ai_chat_turn_service = AIChatTurnService(runtime)
        maintenance_worker = MemoryMaintenanceWorker(
            session_factory=SessionLocal,
            batch_size=getattr(settings, "memory_maintenance_batch_size", 10),
            poll_interval_seconds=getattr(settings, "memory_maintenance_poll_interval_seconds", 2.0),
            timeout_seconds=settings.memory_agent_timeout_seconds,
        )
        await maintenance_worker.start()
        app.state.memory_maintenance_worker = maintenance_worker
        try:
            yield
        finally:
            try:
                await maintenance_worker.stop()
            finally:
                try:
                    runtime.close()
                finally:
                    close_shared_qdrant_clients()

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_origin_regex=r"https?://[^/]+:300[01]", allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(DATA_DIR), check_dir=False), name="media")
for route in (auth_router, bindings_router, care_episodes_router, demo_router, episode_workspace_router, exercise_catalog_router, diagnostics_router, doctor_dashboard_router, doctor_profile_router, professional_care_router, memory_documents_router, streaming_router, ws_router):
    app.include_router(route)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}

@app.get("/ai/chat/health")
def ai_chat_health() -> dict[str, object]:
	evaluation_mode = evaluator_mode_enabled()
	return {
		"status": "ok",
		"service": "ai-chat",
		"transport": "http",
		"environment": settings.environment,
		"backend_revision": settings.build_revision or backend_source_revision(),
		"workflow_version": WORKFLOW_VERSION,
		"capabilities": {
			"evaluation_telemetry": evaluation_mode and settings.debug,
			"evaluation_fixture_control": evaluation_mode,
			"restart_control": evaluation_mode and os.getenv("REHAB_EVAL_RESTART_CONTROL", "").strip().lower() in {"1", "true", "yes"},
		},
		"identity": settings.evaluation_identity,
		"dirty_state": os.getenv("REHAB_EVAL_BACKEND_DIRTY_STATE", "not-applicable"),
	}
