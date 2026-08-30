import logging

from aiohttp import web

from .routes import handle_create_session, handle_delete_file, handle_delete_session, handle_execute, handle_get_file, handle_health, handle_put_file
from .sessions import SessionManager

LOGGER = logging.getLogger(__name__)


async def on_startup(app: web.Application) -> None:
    session_manager: SessionManager = app["session_manager"]
    session_manager.start_sweep()
    LOGGER.info("Server startup complete: session sweep task started")


async def on_cleanup(app: web.Application) -> None:
    session_manager: SessionManager = app["session_manager"]
    await session_manager.stop_sweep()
    await session_manager.delete_all()
    LOGGER.info("Server shutdown complete: all sessions deleted")


def create_app() -> web.Application:
    app = web.Application()
    app["session_manager"] = SessionManager()

    app.router.add_post("/sessions", handle_create_session)
    app.router.add_delete("/sessions/{session_id}", handle_delete_session)
    app.router.add_get("/sessions/{session_id}/files/{path:.*}", handle_get_file)
    app.router.add_put("/sessions/{session_id}/files/{path:.*}", handle_put_file)
    app.router.add_delete("/sessions/{session_id}/files/{path:.*}", handle_delete_file)
    app.router.add_post("/execute", handle_execute)
    app.router.add_get("/health", handle_health)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
