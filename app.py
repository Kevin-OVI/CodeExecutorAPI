import argparse
import logging

from aiohttp import web

from code_executor_api import HOST, PORT, create_app

LOGGER = logging.getLogger(__name__)


class HealthFilterAccessLogger(web.AccessLogger):
    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if request.path == "/health":
            return
        super().log(request, response, time)


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--port must be an integer.") from exc

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--port must be between 1 and 65535.")

    return port


def _parse_host(value: str) -> str:
    host = value.strip()
    if not host:
        raise argparse.ArgumentTypeError("--host must not be empty.")
    return host


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CodeExecutorAPI server.")
    parser.add_argument("--host", type=_parse_host, help="Host interface to bind.")
    parser.add_argument("--port", type=_parse_port, help="Port to listen on.")
    return parser.parse_args()


def main():
    args = _parse_args()
    host = args.host if args.host is not None else HOST
    port = args.port if args.port is not None else PORT

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    LOGGER.info(f"Starting CodeExecutorAPI server on {host}:{port}")
    web.run_app(
        create_app(),
        host=host,
        port=port,
        access_log_class=HealthFilterAccessLogger,
    )


if __name__ == "__main__":
    main()
