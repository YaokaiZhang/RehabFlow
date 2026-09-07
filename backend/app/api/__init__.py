from app.api.auth import router as auth_router
from app.api.bindings import router as bindings_router
from app.api.diagnostics import router as diagnostics_router
from app.api.ws import router as ws_router

__all__ = ["auth_router", "bindings_router", "diagnostics_router", "ws_router"]
