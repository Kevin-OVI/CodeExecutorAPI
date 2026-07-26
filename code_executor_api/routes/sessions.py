import logging
import os

from aiohttp import web

from ..sessions import SessionLockTimeout, SessionManager, SessionNotFound

__all__ = ("handle_create_session", "handle_delete_session")
LOGGER = logging.getLogger(__name__)


def _write_file_sync(full_path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(content)


async def handle_create_session(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session = session_manager.create()

    content_type = request.content_type
    if content_type == "multipart/form-data":
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.filename is None:
                    continue
                await session.write_file(part.filename, part)
        except Exception:
            await session_manager.delete(session.id)
            raise

    return web.json_response({"session_id": session.id})


async def handle_delete_session(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id = request.match_info["session_id"]

    try:
        async with session_manager.locked(session_id):
            await session_manager.delete(session_id)
    except SessionNotFound:
        return web.json_response({"error": "Session not found"}, status=404)
    except SessionLockTimeout:
        return web.json_response({"error": "Session is busy"}, status=409)

    return web.Response(status=204)
