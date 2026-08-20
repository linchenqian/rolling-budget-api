import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from rolling_budget_api import __version__
from rolling_budget_api.api.router import api_router
from rolling_budget_api.core.config import Settings, get_settings
from rolling_budget_api.db.session import get_session_factory
from rolling_budget_api.services.errors import DomainError

logger = logging.getLogger("rolling_budget_api")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    logging.basicConfig(
        level=resolved.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    mcp_server: Any | None = None
    mcp_application: Any | None = None
    oauth_router: Any | None = None
    if resolved.public_base_url is not None:
        from rolling_budget_api.mcp import create_mcp_server
        from rolling_budget_api.oauth import (
            DatabaseTokenVerifier,
            OAuthConfig,
            create_oauth_router,
        )

        oauth_config = OAuthConfig.from_settings(resolved)
        session_factory = get_session_factory(resolved.database_url)
        token_verifier = DatabaseTokenVerifier(oauth_config, session_factory=session_factory)
        mcp_server = create_mcp_server(resolved, token_verifier)
        mcp_application = mcp_server.streamable_http_app()
        oauth_router = create_oauth_router(oauth_config, session_factory=session_factory)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        if mcp_server is None:
            yield
            return
        async with mcp_server.session_manager.run():
            yield

    application = FastAPI(
        title="Rolling Budget API",
        version=__version__,
        description=(
            "Integrity-first ingestion and rolling budget summaries. "
            "Transaction descriptions are never included in application logs."
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved

    @application.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code},
        )

    if resolved.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "PUT", "POST", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "Mcp-Protocol-Version",
                "Mcp-Session-Id",
                "X-Request-ID",
            ],
            expose_headers=["ETag", "Mcp-Session-Id", "X-Request-ID"],
            max_age=600,
        )

    @application.middleware("http")
    async def request_safety(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_limit = (
                    resolved.mcp_max_request_bytes
                    if request.url.path == "/mcp"
                    else resolved.max_request_bytes
                )
                too_large = int(content_length) > request_limit
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if too_large:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_complete method=%s path=%s status=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            request_id,
        )
        return response

    application.include_router(api_router)
    if oauth_router is not None:
        application.include_router(oauth_router)
    if mcp_application is not None:
        # Keep this catch-all mount last so the REST API and health routes win.
        application.mount("/", mcp_application)
    return application


app = create_app()
