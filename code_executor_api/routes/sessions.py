import logging
import os

from aiohttp import web

from ..config import MAX_SESSION_SIZE
from ..file_helpers import ContentSizeLimiter
from ..sessions import (
    QuotaSetupFailed,
    SessionLimitReached,
    SessionLockTimeout,
    SessionManager,
    SessionNotFound,
    SessionResourceLimitReached,
)

__all__ = ("handle_create_session", "handle_delete_session")
LOGGER = logging.getLogger(__name__)


def _write_file_sync(full_path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(content)


async def handle_create_session(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    try:
        session = await session_manager.create()
    except SessionLimitReached:
        return web.json_response({"error": "Session capacity reached"}, status=503)
    except QuotaSetupFailed:
        LOGGER.exception("Failed to apply session storage quota")
        return web.json_response({"error": "Session storage quota setup failed"}, status=500)

    content_type = request.content_type
    if content_type == "multipart/form-data":
        size_limiter = ContentSizeLimiter(MAX_SESSION_SIZE)
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.filename is None:
                    raise web.HTTPBadRequest(text="Multipart parts must be files")
                await session.write_file(part.filename, part, size_limiter)
        except SessionResourceLimitReached as exc:
            await session_manager.delete(session.id)
            return web.json_response({"error": str(exc)}, status=413)
        except Exception:
            await session_manager.delete(session.id)
            raise

    return web.json_response({"session_id": session.id})


async def handle_delete_session(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id = request.match_info["session_id"]

    try:
        await session_manager.delete(session_id)
    except SessionNotFound:
        return web.json_response({"error": "Session not found"}, status=404)
    except SessionLockTimeout:
        return web.json_response({"error": "Session is busy"}, status=409)

    return web.Response(status=204)
