import asyncio
import contextlib
import dataclasses
import logging
import os
import secrets
import shutil
import tempfile
import time
from typing import Any, AsyncGenerator

from aiohttp import IOBasePayload

from .config import SESSION_INACTIVITY_TIMEOUT_SECONDS, SESSION_LOCK_WAIT_TIMEOUT_SECONDS, SESSION_SWEEP_INTERVAL_SECONDS
from .file_helpers import SupportedContentType, read_file, write_file_at_content
from .validation import normalize_sub_path

LOGGER = logging.getLogger(__name__)


class SessionLockTimeout(Exception):
    pass


class SessionNotFound(Exception):
    pass


@dataclasses.dataclass
class Session:
    id: str
    work_directory: str
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    last_used: float = dataclasses.field(default_factory=time.monotonic)

    def _get_full_path(self, sub_path: str, create_dir: bool) -> str:
        path = os.path.join(self.work_directory, normalize_sub_path(sub_path))
        if create_dir:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def read_file(self, sub_path: str) -> IOBasePayload:
        normalised_path = normalize_sub_path(sub_path)
        full_path = os.path.join(self.work_directory, normalised_path)
        return read_file(full_path, filename=normalised_path)

    def import_file(self, sub_path: str, src_path: str):
        full_path = self._get_full_path(sub_path, True)
        os.rename(src_path, full_path)

    async def write_file(self, sub_path: str, content: SupportedContentType):
        full_path = self._get_full_path(sub_path, True)
        await write_file_at_content(full_path, content)

    def delete_file(self, sub_path: str):
        full_path = self._get_full_path(sub_path, False)
        os.remove(full_path)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._sweep_task: asyncio.Task | None = None

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def create(self) -> Session:
        session_id = secrets.token_urlsafe(16)
        work_directory = tempfile.mkdtemp(prefix="code_executor_session_")
        session = Session(id=session_id, work_directory=work_directory)
        self._sessions[session_id] = session
        return session

    async def delete(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionNotFound(session_id)

        def log_errors(function, path, excinfo):
            LOGGER.warning("Could not delete %r using %s:", path, function.__name__, exc_info=excinfo)

        await asyncio.to_thread(shutil.rmtree, session.work_directory, onexc=log_errors)

    @contextlib.asynccontextmanager
    async def locked(self, session_id: str) -> AsyncGenerator[Session, Any]:
        session = self.get(session_id)
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=SESSION_LOCK_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            raise SessionLockTimeout(session_id) from None
        session.last_used = time.monotonic()
        try:
            yield session
        finally:
            session.lock.release()

    async def _sweep_once(self) -> None:
        now = time.monotonic()
        expired_ids = [
            session_id for session_id, session in self._sessions.items()
            if not session.lock.locked() and now - session.last_used > SESSION_INACTIVITY_TIMEOUT_SECONDS
        ]
        for session_id in expired_ids:
            try:
                await self.delete(session_id)
                LOGGER.info("Swept expired session %s", session_id)
            except SessionNotFound:
                pass

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SESSION_SWEEP_INTERVAL_SECONDS)
            try:
                await self._sweep_once()
            except Exception as e:
                LOGGER.exception("Error while sweeping expired sessions", exc_info=e)

    def start_sweep(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop_sweep(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None

    async def delete_all(self) -> None:
        for session_id in list(self._sessions.keys()):
            with contextlib.suppress(SessionNotFound):
                await self.delete(session_id)
