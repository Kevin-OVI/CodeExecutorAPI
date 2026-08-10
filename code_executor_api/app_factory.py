import ipaddress
import logging
import secrets

from aiohttp import web

from .config import API_TOKEN, MAX_REQUEST_SIZE
from .executor import check_docker_environment
from .routes import handle_create_session, handle_delete_file, handle_delete_session, handle_execute, handle_get_file, handle_health, handle_put_file
from .sessions import SessionManager

LOGGER = logging.getLogger(__name__)


def _is_loopback(remote: str | None) -> bool:
    if remote is None:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return remote == "localhost"


@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.path != "/health":
        authorization = request.headers.get("Authorization", "")
        if API_TOKEN is not None:
            if not secrets.compare_digest(authorization, f"Bearer {API_TOKEN}"):
                return web.json_response(
                    {"error": "Authentication required"},
                    status=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        elif not _is_loopback(request.remote):
            return web.json_response({"error": "Authentication required"}, status=401)

    return await handler(request)


async def on_startup(app: web.Application) -> None:
    await check_docker_environment()
    session_manager: SessionManager = app["session_manager"]
    session_manager.start_sweep()
    LOGGER.info("Server startup complete: session sweep task started")


async def on_cleanup(app: web.Application) -> None:
    session_manager: SessionManager = app["session_manager"]
    await session_manager.stop_sweep()
    await session_manager.delete_all()
    LOGGER.info("Server shutdown complete: all sessions deleted")


def create_app() -> web.Application:
    app = web.Application(middlewares=(security_middleware,), client_max_size=MAX_REQUEST_SIZE)
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
