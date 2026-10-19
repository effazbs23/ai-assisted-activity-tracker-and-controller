from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.llm import build_llm
from app.agent.mcp import MCPHub
from app.agent.orchestrator import Orchestrator
from app.api.routes import router
from app.config import Settings
from app.erp.client import ERPError, client_factory
from app.security import AuditLog, RateLimiter
from app.store.actions import ActionStore
from app.store.sessions import SessionStore


def create_app(settings: Settings | None = None, erp_for=None, llm=None, mcp=None) -> FastAPI:
    """erp_for: callable caller -> ERP client (injectable for tests); llm: object with chat(messages, tools);
    mcp: MCP fallback hub (default: built from MCP_SERVERS; None when no servers are configured)."""
    settings = settings or Settings.from_env()
    if mcp is None and settings.mcp_servers:
        mcp = MCPHub(settings.mcp_servers, settings.mcp_timeout)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if mcp is not None:
            mcp.close()  # stop the MCP server child processes

    app = FastAPI(title="ERPNext Activity Assistant", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.erp_for = erp_for or client_factory(settings)
    app.state.actions = ActionStore(settings.action_ttl_seconds)
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.audit = AuditLog(settings.audit_log_path)
    app.state.mcp = mcp  # servers start lazily, on the first request that needs the fallback
    app.state.sessions = SessionStore(settings.session_ttl_seconds, settings.session_max_messages)
    app.state.agent = Orchestrator(app.state, llm or build_llm(settings))
    app.include_router(router)

    @app.exception_handler(ERPError)
    def erp_error(_: Request, exc: ERPError):
        return JSONResponse({"detail": f"ERPNext is unavailable or rejected the request: {exc}"}, status_code=502)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def app_from_env() -> FastAPI:  # uvicorn app.main:app_from_env --factory
    return create_app()
