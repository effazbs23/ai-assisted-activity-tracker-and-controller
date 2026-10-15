from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings
from app.odoo.client import OdooClient, OdooError
from app.security import AuditLog, RateLimiter
from app.agent.llm import GroqLLM
from app.agent.orchestrator import Orchestrator
from app.store.actions import ActionStore
from app.store.sessions import SessionStore


def create_app(settings: Settings | None = None, odoo=None, llm=None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Odoo Middleware", version="0.1.0")
    app.state.settings = settings
    app.state.odoo = odoo or OdooClient(settings)
    app.state.actions = ActionStore(settings.action_ttl_seconds)
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.audit = AuditLog(settings.audit_log_path)
    app.state.sessions = SessionStore(settings.session_ttl_seconds, settings.session_max_messages)
    app.state.agent = Orchestrator(app.state, llm or GroqLLM(settings.groq_key, settings.groq_chat_model))
    app.include_router(router)

    @app.exception_handler(OdooError)
    def odoo_error(_: Request, exc: OdooError):
        return JSONResponse({"detail": "Odoo is unavailable or rejected the request"}, status_code=502)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
