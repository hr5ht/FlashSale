from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppError, app_error_handler, unexpected_error_handler
from app.core.middleware import BodySizeLimit, SecurityHeaders
from app.db.session import engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
# Vercel only bundles backend/, so its build step copies ../frontend into backend/frontend.
FRONTEND_DIR = next(
    (path for path in (BACKEND_DIR / "frontend", BACKEND_DIR.parent / "frontend") if path.is_dir()),
    BACKEND_DIR.parent / "frontend",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    show_docs = settings.vercel_env != "production"

    app = FastAPI(
        title="Flash Sale Ticketing",
        lifespan=lifespan,
        openapi_url="/openapi.json" if show_docs else None,
    )

    app.add_middleware(SecurityHeaders)
    app.add_middleware(BodySizeLimit)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unexpected_error_handler)

    app.include_router(health.router)
    app.include_router(api_router)
    app.frontend("/", directory=FRONTEND_DIR)
    return app


app = create_app()
