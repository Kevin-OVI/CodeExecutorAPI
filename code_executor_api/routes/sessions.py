import logging

from aiohttp import web
from aiohttp.http_exceptions import BadHttpMessage

from ..sessions import (
    QuotaSetupFailed,
    SessionLimitReached,
    SessionLockTimeout,
    SessionManager,
    SessionNotFound,
    SessionResourceLimitReached,
)
from ..validation import ValidationError

__all__ = ("handle_create_session", "handle_delete_session")
LOGGER = logging.getLogger(__name__)


async def handle_create_session(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    try:
        # Seeding runs under the new session's lock, so a concurrent create() cannot evict it
        # mid-upload. Any exception escaping the block deletes the session before it propagates.
        async with session_manager.create_locked() as session:
            if request.content_type == "multipart/form-data":
                reader = await request.multipart()
                async for part in reader:
                    if part.filename is None:
                        raise ValidationError("Multipart parts must be files")
                    await session.write_file(part.filename, part)
    except SessionLimitReached:
        return web.json_response({"error": "Session capacity reached"}, status=503)
    except QuotaSetupFailed:
        LOGGER.exception("Failed to apply session storage quota")
        return web.json_response({"error": "Session storage quota setup failed"}, status=500)
    except SessionResourceLimitReached as exc:
        return web.json_response({"error": str(exc)}, status=413)
    except BadHttpMessage:
        # A part header aiohttp cannot parse - a NUL in a seed filename, say. It carries a
        # 400 code but is not an HTTPException, so without this it would surface as a 500.
        return web.json_response({"error": "Request body is not valid multipart/form-data"}, status=400)

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
