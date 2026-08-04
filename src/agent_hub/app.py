"""FastAPI application factory."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create the Agent Hub ASGI application."""
    application = FastAPI(title="Agent Hub", version="0.1.0")

    @application.get("/health/live", tags=["system"])
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
