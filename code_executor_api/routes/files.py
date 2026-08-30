from aiohttp import web

from ..config import MAX_SESSION_SIZE
from ..file_helpers import ContentSizeLimiter
from ..sessions import SessionLockTimeout, SessionManager, SessionNotFound, SessionResourceLimitReached

__all__ = ("handle_get_file", "handle_put_file", "handle_delete_file")


def _resolve_sub_path(request: web.Request) -> str:
    return request.match_info["path"]


async def handle_get_file(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id = request.match_info["session_id"]
    sub_path = _resolve_sub_path(request)

    try:
        async with session_manager.locked(session_id) as session:
            try:
                content = session.read_file(sub_path)
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                return web.json_response({"error": "File not found"}, status=404)
    except SessionNotFound:
        return web.json_response({"error": "Session not found"}, status=404)
    except SessionLockTimeout:
        return web.json_response({"error": "Session is busy"}, status=409)

    return web.Response(body=content, content_type="application/octet-stream")


async def handle_put_file(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id = request.match_info["session_id"]
    sub_path = _resolve_sub_path(request)

    try:
        async with session_manager.locked(session_id) as session:
            try:
                await session.write_file(sub_path, request.content, ContentSizeLimiter(MAX_SESSION_SIZE))
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                return web.json_response({"error": "Invalid file path"}, status=400)
    except SessionNotFound:
        return web.json_response({"error": "Session not found"}, status=404)
    except SessionLockTimeout:
        return web.json_response({"error": "Session is busy"}, status=409)
    except SessionResourceLimitReached as exc:
        return web.json_response({"error": str(exc)}, status=413)

    return web.Response(status=204)


async def handle_delete_file(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id = request.match_info["session_id"]
    sub_path = _resolve_sub_path(request)

    try:
        async with session_manager.locked(session_id) as session:
            try:
                session.delete_file(sub_path)
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                return web.json_response({"error": "File not found"}, status=404)
    except SessionNotFound:
        return web.json_response({"error": "Session not found"}, status=404)
    except SessionLockTimeout:
        return web.json_response({"error": "Session is busy"}, status=409)

    return web.Response(status=204)
