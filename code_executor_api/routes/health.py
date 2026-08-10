from aiohttp import web

from ..executor import check_docker_environment

__all__ = ("handle_health",)


async def handle_health(_request: web.Request) -> web.Response:
    try:
        await check_docker_environment()
    except RuntimeError as exc:
        return web.json_response({"status": "unavailable", "error": str(exc)}, status=503)
    return web.json_response({"status": "ok"})
