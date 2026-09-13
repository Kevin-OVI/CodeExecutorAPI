import argparse
import logging

from aiohttp import web

from code_executor_api import HOST, PORT, SESSION_QUOTA_MOUNTPOINT, create_app

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
    parser.add_argument(
        "--no-session-quota", action="store_true",
        help="Start without SESSION_QUOTA_MOUNTPOINT, leaving MAX_SESSION_SIZE and MAX_SESSION_ENTRIES "
             "unenforced. For development only: sessions can then grow until the filesystem fills up. "
             "Ignored when SESSION_QUOTA_MOUNTPOINT is set.",
    )
    args = parser.parse_args()

    # The XFS project quota is the only thing enforcing MAX_SESSION_SIZE and
    # MAX_SESSION_ENTRIES, so starting without it has to be a deliberate choice rather than
    # the accident of an unset variable.
    if SESSION_QUOTA_MOUNTPOINT is None and not args.no_session_quota:
        parser.error(
            "Missing environment variable SESSION_QUOTA_MOUNTPOINT. It is what enforces "
            "MAX_SESSION_SIZE and MAX_SESSION_ENTRIES, so without it a session can fill the "
            "filesystem. Set it to the XFS mount point containing SESSION_ROOT_DIRECTORY, or "
            "pass --no-session-quota to start without per-session limits (development only)."
        )

    return args


def main():
    args = _parse_args()
    host = args.host if args.host is not None else HOST
    port = args.port if args.port is not None else PORT

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if SESSION_QUOTA_MOUNTPOINT is None:
        LOGGER.warning(
            "Starting without a session quota (--no-session-quota): MAX_SESSION_SIZE and "
            "MAX_SESSION_ENTRIES are not enforced and sessions can fill the filesystem."
        )
    LOGGER.info(f"Starting CodeExecutorAPI server on {host}:{port}")
    web.run_app(
        create_app(),
        host=host,
        port=port,
        access_log_class=HealthFilterAccessLogger,
    )


if __name__ == "__main__":
    main()
