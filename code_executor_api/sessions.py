import asyncio
import contextlib
import dataclasses
import errno
import logging
import os
import posixpath
import secrets
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, AsyncGenerator

from aiohttp import IOBasePayload
from aiohttp.web import HTTPRequestEntityTooLarge

from .config import (
    CONTAINER_ULIMIT_FSIZE,
    DOCKER_CHECK_TIMEOUT_SECONDS,
    MAX_CONCURRENT_EXECUTIONS,
    MAX_SESSIONS,
    MAX_SESSION_SIZE,
    SESSION_INACTIVITY_TIMEOUT_SECONDS,
    SESSION_LOCK_WAIT_TIMEOUT_SECONDS,
    SESSION_QUOTA_MOUNTPOINT,
    SESSION_ROOT_DIRECTORY,
    SESSION_SWEEP_INTERVAL_SECONDS,
)
from .file_helpers import ContentSizeLimiter, SupportedContentType, read_file, write_file_to_temp
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


class QuotaSetupFailed(Exception):
    pass


# Basic-block-aligned range reserved for XFS project ids assigned to sessions; low ids are
# conventionally left free for other uses of project quotas on the same filesystem.
_PROJECT_ID_BASE = 1000
_PROJECT_ID_MAX = 2 ** 31 - 1


async def _run_xfs_quota(*args: str) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "xfs_quota", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise QuotaSetupFailed(f"Failed to invoke xfs_quota: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise QuotaSetupFailed(f"xfs_quota timed out: {' '.join(args)}") from None

    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _apply_quota(work_directory: str, project_id: int) -> None:
    if SESSION_QUOTA_MOUNTPOINT is None:
        return

    # xfs_quota's `b` suffix means 512-byte basic blocks, not bytes, so bhard must be given in
    # KiB (`k`) instead — rounded up so the enforced limit is never looser than MAX_SESSION_SIZE.
    bhard_kib = -(-MAX_SESSION_SIZE // 1024)

    returncode, stdout, stderr = await _run_xfs_quota(
        "-x",
        "-c", f"project -s -p {work_directory} {project_id}",
        "-c", f"limit -p bhard={bhard_kib}k {project_id}",
        SESSION_QUOTA_MOUNTPOINT,
    )
    if returncode != 0:
        raise QuotaSetupFailed(
            f"xfs_quota setup failed for project {project_id}: {(stdout + stderr).strip()}"
        )

    # `limit`/`project -s` can fail to actually register a hard limit while still exiting 0 and
    # printing only the routine "Setting up project ... Processed N paths ..." success banner
    # (e.g. when project quota accounting isn't enabled on the target filesystem), so confirm
    # the limit really landed rather than trusting the returncode alone.
    returncode, stdout, stderr = await _run_xfs_quota("-x", "-c", "report -p -N -b", SESSION_QUOTA_MOUNTPOINT)
    if returncode != 0:
        raise QuotaSetupFailed(
            f"xfs_quota verification failed for project {project_id}: {(stdout + stderr).strip()}"
        )

    for line in stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == f"#{project_id}":
            if len(fields) < 4 or not fields[3].isdigit() or int(fields[3]) <= 0:
                raise QuotaSetupFailed(
                    f"xfs_quota reports no hard limit set for project {project_id}: {line.strip()}"
                )
            return

    raise QuotaSetupFailed(f"xfs_quota project {project_id} not found in report after setup")


async def _try_acquire_lock(lock: asyncio.Lock) -> bool:
    try:
        await asyncio.wait_for(lock.acquire(), timeout=sys.float_info.min)
    except TimeoutError:
        return False
    return True


@dataclasses.dataclass
class Session:
    id: str
    work_directory: str
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    last_used: float = dataclasses.field(default_factory=time.monotonic)
    files_size: dict[str, int] = dataclasses.field(default_factory=dict)
    project_id: int | None = None

    @property
    def total_file_size(self) -> int:
        return sum(self.files_size.values())

    def get_maximum_allowed_size(self, sub_path: str) -> int:
        return MAX_SESSION_SIZE - self.total_file_size + self.files_size.get(sub_path, 0)

    @contextlib.contextmanager
    def _open_parent(self, normalised_sub_path: str, create_dir: bool):
        path_parts = normalised_sub_path.split("/")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent_fd = os.open(self.work_directory, flags)
        try:
            for path_part in path_parts[:-1]:
                if create_dir:
                    try:
                        os.mkdir(path_part, 0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(path_part, flags, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise FileNotFoundError(normalised_sub_path) from None
                    raise
                os.close(parent_fd)
                parent_fd = next_fd
            yield parent_fd, path_parts[-1], normalised_sub_path
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

    def read_file(self, sub_path: str, field_name: str | None = None) -> IOBasePayload:
        normalised_sub_path = normalize_sub_path(sub_path)
        with self._open_parent(normalised_sub_path, False) as (parent_fd, filename, normalised_sub_path):
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(filename, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise FileNotFoundError(normalised_sub_path) from None
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise FileNotFoundError(normalised_sub_path)
                return read_file(fd, filename=normalised_sub_path, field_name=field_name)
            except BaseException:
                os.close(fd)
                raise

    async def stage_file(self, sub_path: str, content: SupportedContentType, size_limiter: ContentSizeLimiter) -> tuple[str, str]:
        """Write `content` to a hidden temp file inside sub_path's parent directory, without
        making it visible under `sub_path` yet. Returns (normalised_sub_path, temporary_name)
        to later pass to commit_staged_file (reveal it) or discard_staged_file (drop it)."""
        normalised_sub_path = normalize_sub_path(sub_path)
        size_limiter = size_limiter.reduced_max(self.get_maximum_allowed_size(normalised_sub_path))
        try:
            with self._open_parent(normalised_sub_path, True) as (parent_fd, _, _):
                temporary_name, _ = await write_file_to_temp(parent_fd, content, CONTAINER_ULIMIT_FSIZE, size_limiter)
        except HTTPRequestEntityTooLarge:
            # Same outward error (413, JSON body) whether the tracked-bytes limiter or the
            # OS-enforced XFS quota below is what actually rejected the write.
            raise SessionResourceLimitReached("Session storage limit reached") from None
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise SessionResourceLimitReached("Session storage limit reached") from None
            raise
        return normalised_sub_path, temporary_name

    async def commit_staged_file(self, normalised_sub_path: str, temporary_name: str) -> None:
        with self._open_parent(normalised_sub_path, False) as (parent_fd, filename, _):
            size = os.stat(temporary_name, dir_fd=parent_fd).st_size
            os.replace(temporary_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        self.files_size[normalised_sub_path] = size

    async def discard_staged_file(self, normalised_sub_path: str, temporary_name: str) -> None:
        with self._open_parent(normalised_sub_path, False) as (parent_fd, _, _):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)

    async def write_file(self, sub_path: str, content: SupportedContentType, size_limiter: ContentSizeLimiter):
        normalised_sub_path, temporary_name = await self.stage_file(sub_path, content, size_limiter)
        try:
            await self.commit_staged_file(normalised_sub_path, temporary_name)
        except OSError as exc:
            with contextlib.suppress(OSError):
                await self.discard_staged_file(normalised_sub_path, temporary_name)
            if exc.errno == errno.ENOSPC:
                raise SessionResourceLimitReached("Session storage limit reached") from None
            raise

    def delete_file(self, sub_path: str):
        normalised_sub_path = normalize_sub_path(sub_path)
        with self._open_parent(normalised_sub_path, False) as (parent_fd, filename, _):
            if stat.S_ISDIR(os.stat(filename, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                raise IsADirectoryError(filename)
            os.unlink(filename, dir_fd=parent_fd)
            self.files_size.pop(normalised_sub_path, None)

    def get_sub_path(self, full_path: str):
        normalised_path = posixpath.normpath(full_path.replace("\\", "/"))
        if not normalised_path.startswith(self.work_directory + "/"):
            raise ValueError(f"Path {normalised_path!r} is not within the session directory ({self.work_directory!r})")
        return normalised_path[len(self.work_directory) + 1:]


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._sweep_task: asyncio.Task | None = None
        self._execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)
        self._next_project_id = _PROJECT_ID_BASE
        self._free_project_ids: list[int] = []

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def _allocate_project_id(self) -> int:
        if self._free_project_ids:
            return self._free_project_ids.pop()
        project_id = self._next_project_id
        if project_id > _PROJECT_ID_MAX:
            raise QuotaSetupFailed("Exhausted available XFS project ids")
        self._next_project_id += 1
        return project_id

    def _release_project_id(self, project_id: int | None) -> None:
        if project_id is not None:
            self._free_project_ids.append(project_id)

    async def create(self) -> Session:
        if len(self._sessions) >= MAX_SESSIONS:
            raise SessionLimitReached
        session_id = secrets.token_urlsafe(16)
        work_directory = tempfile.mkdtemp(prefix="code_executor_session_", dir=SESSION_ROOT_DIRECTORY)
        project_id = self._allocate_project_id() if SESSION_QUOTA_MOUNTPOINT is not None else None

        # Reserve the session slot (and the MAX_SESSIONS budget) synchronously, before the
        # first `await` below, so a concurrent create() can't slip past the capacity check.
        session = Session(id=session_id, work_directory=work_directory, project_id=project_id)
        self._sessions[session_id] = session
        try:
            if project_id is not None:
                await _apply_quota(work_directory, project_id)
        except BaseException:
            self._sessions.pop(session_id, None)
            self._release_project_id(project_id)
            await asyncio.to_thread(shutil.rmtree, work_directory, ignore_errors=True)
            raise
        return session

    async def _delete(self, session: Session) -> None:
        await asyncio.to_thread(shutil.rmtree, session.work_directory, ignore_errors=True)
        self._release_project_id(session.project_id)
        self._sessions.pop(session.id)

    async def delete(self, session_id: str) -> None:
        async with self.locked(session_id) as session:
            await self._delete(session)

    @contextlib.asynccontextmanager
    async def locked(self, session_id: str) -> AsyncGenerator[Session, Any]:
        session = self.get(session_id)
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=SESSION_LOCK_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            raise SessionLockTimeout(session_id) from None
        try:
            if self.get(session_id) is not session:
                raise SessionNotFound(session_id)
            session.last_used = time.monotonic()
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
        expired_sessions: list[Session] = []
        for session in self._sessions.values():
            if not await _try_acquire_lock(session.lock):
                continue
            if now - session.last_used <= SESSION_INACTIVITY_TIMEOUT_SECONDS:
                session.lock.release()
                continue
            expired_sessions.append(session)

        for session in expired_sessions:
            try:
                await self._delete(session)
            except Exception as e:
                LOGGER.exception("Failed to sweep expired session %s", session.id, exc_info=e)
            else:
                LOGGER.info("Swept expired session %s", session.id)

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
        for session in list(self._sessions.values()):
            await self._delete(session)
