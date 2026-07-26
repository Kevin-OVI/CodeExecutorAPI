from aiohttp import web

__all__ = ("handle_health",)


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
