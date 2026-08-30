import asyncio
import contextlib
import dataclasses
import errno
import logging
import os
import secrets
import shutil
import stat
import tempfile
import time
from typing import Any, AsyncGenerator

from aiohttp import IOBasePayload

from .config import (
    MAX_CONCURRENT_EXECUTIONS,
    MAX_SESSIONS,
    MAX_SESSION_FILES,
    MAX_SESSION_SIZE,
    CONTAINER_ULIMIT_FSIZE,
    SESSION_INACTIVITY_TIMEOUT_SECONDS,
    SESSION_LOCK_WAIT_TIMEOUT_SECONDS,
    SESSION_ROOT_DIRECTORY,
    SESSION_SWEEP_INTERVAL_SECONDS,
)
from .file_helpers import ContentSizeLimiter, SupportedContentType, read_file, write_file_at_content
from .validation import normalize_sub_path

LOGGER = logging.getLogger(__name__)


class SessionLockTimeout(Exception):
    pass


class SessionNotFound(Exception):
    pass


class SessionLimitReached(Exception):
    pass


class SessionResourceLimitReached(Exception):
    pass


class ExecutionLimitReached(Exception):
    pass


@dataclasses.dataclass
class Session:
    id: str
    work_directory: str
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    last_used: float = dataclasses.field(default_factory=time.monotonic)

    def _get_usage(self) -> tuple[int, int]:
        size = 0
        entries = 0
        for directory, directories, files in os.walk(self.work_directory, followlinks=False):
            if directory == self.work_directory:
                with contextlib.suppress(ValueError):
                    directories.remove(".cache")
            entries += len(directories) + len(files)
            if entries > MAX_SESSION_FILES:
                raise SessionResourceLimitReached("Session contains too many files")
            for filename in files:
                try:
                    file_stat = os.stat(os.path.join(directory, filename), follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(file_stat.st_mode):
                    size += file_stat.st_size
                    if size > MAX_SESSION_SIZE:
                        raise SessionResourceLimitReached("Session storage limit reached")
        return size, entries

    @contextlib.contextmanager
    def _open_parent(self, sub_path: str, create_dir: bool, usage: tuple[int, int] | None = None):
        normalised_path = normalize_sub_path(sub_path)
        path_parts = normalised_path.split("/")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        if create_dir:
            size, entries = usage if usage is not None else self._get_usage()
        else:
            size, entries = 0, 0
        parent_fd = os.open(self.work_directory, flags)
        try:
            for path_part in path_parts[:-1]:
                if create_dir:
                    try:
                        if entries + 1 > MAX_SESSION_FILES:
                            raise SessionResourceLimitReached("Session contains too many files")
                        os.mkdir(path_part, 0o700, dir_fd=parent_fd)
                        entries += 1
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(path_part, flags, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise FileNotFoundError(normalised_path) from None
                    raise
                os.close(parent_fd)
                parent_fd = next_fd
            yield parent_fd, path_parts[-1], normalised_path, size, entries
        finally:
            os.close(parent_fd)

    @staticmethod
    def _get_existing_file(parent_fd: int, filename: str) -> tuple[bool, int]:
        try:
            file_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False, 0
        if stat.S_ISDIR(file_stat.st_mode):
            raise IsADirectoryError(filename)
        return True, file_stat.st_size if stat.S_ISREG(file_stat.st_mode) else 0

    @staticmethod
    def _validate_replacement(size: int, entries: int, existing: bool, existing_size: int, new_size: int) -> None:
        if entries + (0 if existing else 1) > MAX_SESSION_FILES:
            raise SessionResourceLimitReached("Session contains too many files")
        if size - existing_size + new_size > MAX_SESSION_SIZE:
            raise SessionResourceLimitReached("Session storage limit reached")

    def read_file(self, sub_path: str, field_name: str | None = None) -> IOBasePayload:
        with self._open_parent(sub_path, False) as (parent_fd, filename, normalised_path, _, _):
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(filename, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise FileNotFoundError(normalised_path) from None
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise FileNotFoundError(normalised_path)
                return read_file(fd, filename=normalised_path, field_name=field_name)
            except BaseException:
                os.close(fd)
                raise

    async def import_file(self, sub_path: str, src_path: str):
        source_stat = os.stat(src_path, follow_symlinks=False)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("Only regular files can be imported")
        if source_stat.st_size > CONTAINER_ULIMIT_FSIZE:
            raise SessionResourceLimitReached("File size limit reached")
        usage = await asyncio.to_thread(self._get_usage)
        with self._open_parent(sub_path, True, usage=usage) as (parent_fd, filename, _, size, entries):
            existing, existing_size = self._get_existing_file(parent_fd, filename)
            self._validate_replacement(size, entries, existing, existing_size, source_stat.st_size)
            os.replace(src_path, filename, dst_dir_fd=parent_fd)

    async def write_file(self, sub_path: str, content: SupportedContentType, size_limiter: ContentSizeLimiter | None = None):
        usage = await asyncio.to_thread(self._get_usage)
        with self._open_parent(sub_path, True, usage=usage) as (parent_fd, filename, _, size, entries):
            existing, existing_size = self._get_existing_file(parent_fd, filename)

            def validate_size(new_size: int) -> None:
                self._validate_replacement(size, entries, existing, existing_size, new_size)

            await write_file_at_content(parent_fd, filename, content, CONTAINER_ULIMIT_FSIZE, validate_size, size_limiter)

    def delete_file(self, sub_path: str):
        with self._open_parent(sub_path, False) as (parent_fd, filename, _, _, _):
            if stat.S_ISDIR(os.stat(filename, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                raise IsADirectoryError(filename)
            os.unlink(filename, dir_fd=parent_fd)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._sweep_task: asyncio.Task | None = None
        self._execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def create(self) -> Session:
        if len(self._sessions) >= MAX_SESSIONS:
            raise SessionLimitReached
        session_id = secrets.token_urlsafe(16)
        work_directory = tempfile.mkdtemp(prefix="code_executor_session_", dir=SESSION_ROOT_DIRECTORY)
        session = Session(id=session_id, work_directory=work_directory)
        self._sessions[session_id] = session
        return session

    async def delete(self, session_id: str) -> None:
        session = self.get(session_id)
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=SESSION_LOCK_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            raise SessionLockTimeout(session_id) from None
        try:
            # Re-check membership: another caller may have already deleted this session
            # while we were waiting for the lock.
            if self._sessions.get(session_id) is not session:
                raise SessionNotFound(session_id)
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.rmtree, session.work_directory)
            self._sessions.pop(session_id)
        finally:
            session.lock.release()

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

    @contextlib.asynccontextmanager
    async def execution_slot(self) -> AsyncGenerator[None, Any]:
        try:
            await asyncio.wait_for(self._execution_semaphore.acquire(), timeout=SESSION_LOCK_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ExecutionLimitReached from None
        try:
            yield
        finally:
            self._execution_semaphore.release()

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
            except (SessionNotFound, SessionLockTimeout):
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
            with contextlib.suppress(SessionNotFound, SessionLockTimeout):
                await self.delete(session_id)
