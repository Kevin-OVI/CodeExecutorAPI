import asyncio
import contextlib
import dataclasses
import errno
import logging
import os
import posixpath
import secrets
import stat
import sys
import time
from typing import Any, AsyncGenerator

from aiohttp import IOBasePayload
from aiohttp.web import HTTPRequestEntityTooLarge

from .config import (
    CONTAINER_ULIMIT_FSIZE,
    MAX_CONCURRENT_EXECUTIONS,
    MAX_SESSIONS,
    MAX_SESSION_ENTRIES,
    MAX_SESSION_SIZE,
    PODMAN_CHECK_TIMEOUT_SECONDS,
    SESSION_EVICTION_MIN_IDLE_SECONDS,
    SESSION_INACTIVITY_TIMEOUT_SECONDS,
    SESSION_LOCK_WAIT_TIMEOUT_SECONDS,
    SESSION_QUOTA_COMMAND,
    SESSION_QUOTA_MOUNTPOINT,
    SESSION_SWEEP_INTERVAL_SECONDS,
)
from .file_helpers import SupportedContentType, read_file, write_to_fd
from .fs_client import FsHelper
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
        # SESSION_QUOTA_COMMAND is an argv prefix, not a shell string, so a sudo wrapper adds
        # no quoting or injection surface. It defaults to a bare `xfs_quota`, which is what an
        # API relying on file capabilities on the binary needs.
        process = await asyncio.create_subprocess_exec(
            *SESSION_QUOTA_COMMAND, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise QuotaSetupFailed(f"Failed to invoke xfs_quota: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=PODMAN_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise QuotaSetupFailed(f"xfs_quota timed out: {' '.join(args)}") from None

    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _verify_quota_hard_limit(project_id: int, report_flag: str, limit_name: str) -> None:
    """Confirm a hard limit really landed for `project_id`.

    `limit`/`project -s` can fail to actually register a hard limit while still exiting 0 and
    printing only the routine "Setting up project ... Processed N paths ..." success banner
    (e.g. when project quota accounting isn't enabled on the target filesystem), so the
    returncode alone isn't enough. `-N` drops the header and `-n` suppresses the /etc/projid
    name lookup, which is what keeps the `#<id>` match below reliable. The hard limit is
    field 3 for both the block (`-b`) and inode (`-i`) reports.
    """
    assert SESSION_QUOTA_MOUNTPOINT is not None
    returncode, stdout, stderr = await _run_xfs_quota(
        "-x", "-c", f"report -p -N -n {report_flag}", SESSION_QUOTA_MOUNTPOINT
    )
    if returncode != 0:
        raise QuotaSetupFailed(
            f"xfs_quota {limit_name} verification failed for project {project_id}: {(stdout + stderr).strip()}"
        )

    for line in stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == f"#{project_id}":
            if len(fields) < 4 or not fields[3].isdigit() or int(fields[3]) <= 0:
                raise QuotaSetupFailed(
                    f"xfs_quota reports no {limit_name} hard limit set for project {project_id}: {line.strip()}"
                )
            return

    raise QuotaSetupFailed(f"xfs_quota project {project_id} not found in {limit_name} report after setup")


async def _apply_quota(work_directory: str, project_id: int) -> None:
    if SESSION_QUOTA_MOUNTPOINT is None:
        return

    # xfs_quota's `b` suffix means 512-byte basic blocks, not bytes, so bhard must be given in
    # KiB (`k`) instead — rounded up so the enforced limit is never looser than MAX_SESSION_SIZE.
    # ihard takes no unit suffix at all: it is a raw inode count.
    bhard_kib = -(-MAX_SESSION_SIZE // 1024)

    # Both limits in a single `limit` command: that is one quotactl with a combined mask, so the
    # project can't end up with only one of the two applied.
    returncode, stdout, stderr = await _run_xfs_quota(
        "-x",
        "-c", f"project -s -p {work_directory} {project_id}",
        "-c", f"limit -p bhard={bhard_kib}k ihard={MAX_SESSION_ENTRIES} {project_id}",
        SESSION_QUOTA_MOUNTPOINT,
    )
    if returncode != 0:
        raise QuotaSetupFailed(
            f"xfs_quota setup failed for project {project_id}: {(stdout + stderr).strip()}"
        )

    await _verify_quota_hard_limit(project_id, "-b", "block")
    await _verify_quota_hard_limit(project_id, "-i", "inode")


async def _try_acquire_lock(lock: asyncio.Lock) -> bool:
    """Take `lock` if it is free right now, otherwise report failure without waiting.

    `asyncio.Lock` has no non-blocking acquire, and `locked()` is not a substitute: it reports
    a state rather than reserving one, so a queued waiter can be handed the lock in the await
    that follows the check. The smallest positive timeout does both in one step.
    """
    try:
        await asyncio.wait_for(lock.acquire(), timeout=sys.float_info.min)
    except TimeoutError:
        return False
    return True


@dataclasses.dataclass
class Session:
    """One session's directory, plus the lock and bookkeeping that guard it.

    Executed code and the API run as different host users, and the files executed code
    creates are out of the API's reach. Every operation on the tree therefore goes through
    `fs_helper`, which runs inside podman's rootless user namespace, confines each request to
    this session's directory and hands opened descriptors back where content has to flow.
    """

    id: str
    work_directory: str
    fs_helper: FsHelper
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    last_used: float = dataclasses.field(default_factory=time.monotonic)
    project_id: int | None = None

    @property
    def directory_name(self) -> str:
        """The session directory's basename: the name the file helper knows the session by."""
        return os.path.basename(self.work_directory)

    async def _call(self, op: str, **args: Any) -> Any:
        """Run a helper operation on this session that has no descriptor to hand back.

        Any descriptor that arrives anyway is closed, so a protocol mismatch cannot leak one.
        """
        result, fd = await self.fs_helper.call(op, session=self.directory_name, **args)
        if fd is not None:
            os.close(fd)
        return result

    async def read_file(self, sub_path: str, field_name: str | None = None) -> IOBasePayload:
        """Open a regular file in the session as a response payload that owns its descriptor.

        The helper does the confined walk and reports anything with no content to serve - a
        symlink, a socket, a device node with no driver behind it - as `FileNotFoundError`,
        the same not-found an absent file gets. Errnos that mean the *server* is in trouble
        (EMFILE, ENOMEM) stay what they are, and so a 500.
        """
        normalised_sub_path = normalize_sub_path(sub_path)
        _, fd = await self.fs_helper.call("open_read", session=self.directory_name, sub_path=normalised_sub_path)
        if fd is None:
            raise OSError(errno.EIO, "File helper returned no descriptor", normalised_sub_path)
        try:
            file_mode = os.fstat(fd).st_mode
            if stat.S_ISDIR(file_mode):
                # Distinct from "not found" so the files API can serve a listing instead.
                raise IsADirectoryError(normalised_sub_path)
            if not stat.S_ISREG(file_mode):
                raise FileNotFoundError(normalised_sub_path)
        except BaseException:
            os.close(fd)
            raise
        return read_file(fd, filename=normalised_sub_path, field_name=field_name)

    async def list_directory(self, sub_path: str) -> dict[str, Any]:
        """List one directory level, without following symlinks anywhere along the path.

        Unlike execution results this hides nothing: hidden directories and symlinks are
        reported too, so a caller can always discover what a session actually holds. A target
        that is a symlink or not a directory raises `NotADirectoryError`.
        """
        normalised_sub_path = "" if sub_path.strip("/") == "" else normalize_sub_path(sub_path)
        entries = await self._call("list", sub_path=normalised_sub_path)
        return {"path": normalised_sub_path, "entries": entries}

    async def stage_file(self, sub_path: str, content: SupportedContentType) -> tuple[str, str]:
        """Write `content` to a hidden temp file inside sub_path's parent directory, without
        making it visible under `sub_path` yet. Returns (normalised_sub_path, temporary_name)
        to later pass to commit_staged_file (reveal it) or discard_staged_file (drop it).

        The helper creates the temp file - and any missing parent directories - owned by the
        container user, so executed code can use the file once it is committed, and hands back
        a write descriptor that the content is streamed into here.

        The session's total size is not tracked here: MAX_SESSION_SIZE is the XFS project
        quota's job, so a host-mediated write runs into exactly the same kernel limit as one
        made by code running in the container. Only the per-file CONTAINER_ULIMIT_FSIZE cap
        is applied in userspace, matching the `RLIMIT_FSIZE` the container itself runs under.
        """
        normalised_sub_path = normalize_sub_path(sub_path)
        try:
            temporary_name, fd = await self.fs_helper.call(
                "stage", session=self.directory_name, sub_path=normalised_sub_path
            )
            try:
                if fd is None:
                    raise OSError(errno.EIO, "File helper returned no descriptor", normalised_sub_path)
                await write_to_fd(fd, content, CONTAINER_ULIMIT_FSIZE)
            except BaseException:
                # Whatever stopped the write, a partial temp file must not stay behind eating
                # into the session's quota.
                with contextlib.suppress(OSError):
                    await self.discard_staged_file(normalised_sub_path, temporary_name)
                raise
        except HTTPRequestEntityTooLarge:
            # Same outward error (413, JSON body) whether the per-file cap or the OS-enforced
            # XFS quota below is what actually rejected the write.
            raise SessionResourceLimitReached("File size limit reached") from None
        except OSError as exc:
            if exc.errno == errno.ENOSPC or exc.errno == errno.EDQUOT:
                raise SessionResourceLimitReached("Session storage or file count limit reached") from None
            raise
        return normalised_sub_path, temporary_name

    async def commit_staged_file(self, normalised_sub_path: str, temporary_name: str) -> None:
        """Reveal a staged file under `normalised_sub_path`, or discard it if that fails.

        Either way the staged file is dealt with by the time this returns or raises, so callers
        never discard it themselves. Cancelling the await cannot stop the helper, which carries
        on with the rename in its own process: a discard sent meanwhile would run on another
        helper thread and race it. A cancellation therefore waits for the commit to finish, and
        discards only if the commit did not succeed.
        """
        commit = asyncio.ensure_future(
            self._call("commit", sub_path=normalised_sub_path, temp_name=temporary_name)
        )
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError:
            try:
                await commit
            except Exception:
                with contextlib.suppress(OSError):
                    await self.discard_staged_file(normalised_sub_path, temporary_name)
            raise
        except BaseException:
            with contextlib.suppress(OSError):
                await self.discard_staged_file(normalised_sub_path, temporary_name)
            raise

    async def discard_staged_file(self, normalised_sub_path: str, temporary_name: str) -> None:
        await self._call("discard", sub_path=normalised_sub_path, temp_name=temporary_name)

    async def write_file(self, sub_path: str, content: SupportedContentType):
        normalised_sub_path, temporary_name = await self.stage_file(sub_path, content)
        try:
            await self.commit_staged_file(normalised_sub_path, temporary_name)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise SessionResourceLimitReached("Session storage or file count limit reached") from None
            raise

    async def delete_file(self, sub_path: str):
        """Delete one file or symlink; a directory raises `IsADirectoryError`."""
        await self._call("delete", sub_path=normalize_sub_path(sub_path))

    def get_sub_path(self, full_path: str):
        """Convert an absolute host path into a session-relative one, rejecting anything outside.

        Purely textual, so it says nothing about symlinks along the way - it is for paths the
        API itself produced, not for anything a caller supplied.
        """
        normalised_path = posixpath.normpath(full_path)
        if not normalised_path.startswith(self.work_directory + "/"):
            raise ValueError(f"Path {normalised_path!r} is not within the session directory ({self.work_directory!r})")
        return normalised_path[len(self.work_directory) + 1:]


class SessionManager:
    def __init__(self, fs_helper: FsHelper):
        self._fs_helper = fs_helper
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

    async def _claim_eviction_victim(self) -> Session | None:
        """Take the least-recently-used evictable session out of the manager, or return None.

        A session is evictable when nothing holds or is queued on its lock and it has been idle
        for at least SESSION_EVICTION_MIN_IDLE_SECONDS. The victim is returned locked and
        already removed from `self._sessions`, so its slot is free as of the return; retiring
        its directory is left to the caller.
        """
        now = time.monotonic()
        for session in sorted(self._sessions.values(), key=lambda candidate: candidate.last_used):
            if now - session.last_used < SESSION_EVICTION_MIN_IDLE_SECONDS:
                break  # sorted oldest first, so every remaining session is more recent still
            if not await _try_acquire_lock(session.lock):
                continue
            # The try-acquire is an await: the session may have been retired or used meanwhile.
            if (
                self._sessions.get(session.id) is session
                and time.monotonic() - session.last_used >= SESSION_EVICTION_MIN_IDLE_SECONDS
            ):
                del self._sessions[session.id]
                return session
            session.lock.release()
        return None

    async def _create(self) -> Session:
        """Create a session and return it with its lock held, evicting idle ones if full."""
        # Taken before the session is visible to anyone, so it cannot contend, and it keeps the
        # session from being evicted by a concurrent create() before the caller has used it.
        lock = asyncio.Lock()
        await lock.acquire()

        victims: list[Session] = []
        try:
            # Created before the capacity check: creating the directory is an await, and nothing
            # may await between the check and the reservation below.
            directory_name, _ = await self._fs_helper.call("create_session")
            work_directory = os.path.join(self._fs_helper.session_root, directory_name)
            session: Session | None = None
            project_id: int | None = None
            try:
                # Each claim is an await, so a concurrent create() can take the slot it frees;
                # keep claiming until one is free when control comes back here. Returning from
                # the awaited claim does not yield to the event loop, so the slot it freed is
                # still free at the reservation below.
                while len(self._sessions) >= MAX_SESSIONS:
                    victim = await self._claim_eviction_victim()
                    if victim is None:
                        raise SessionLimitReached
                    victims.append(victim)

                session_id = secrets.token_urlsafe(16)
                project_id = self._allocate_project_id() if SESSION_QUOTA_MOUNTPOINT is not None else None

                # Reserve the session slot (and the MAX_SESSIONS budget) synchronously, before
                # the next `await`, so a concurrent create() can't slip past the capacity check.
                session = Session(
                    id=session_id,
                    work_directory=work_directory,
                    fs_helper=self._fs_helper,
                    project_id=project_id,
                    lock=lock,
                )
                self._sessions[session_id] = session
                # The helper creates the directory owned by the service account, and only hands it
                # over to the container user once the quota is in place. The order matters:
                # xfs_quota runs as the service account, and when it does so through file
                # capabilities - CAP_SYS_ADMIN, which overrides no permission check - it can only
                # open and tag the directory while the service account still owns it. Handing it
                # over last also means executed code never gets a directory without its quota.
                if project_id is not None:
                    await _apply_quota(work_directory, project_id)
                await session._call("hand_over")
            except BaseException:
                if session is not None:
                    self._sessions.pop(session.id, None)
                self._release_project_id(project_id)
                # Best-effort: no code has run against this directory yet, and the original
                # failure is the one to raise.
                with contextlib.suppress(OSError):
                    await self._fs_helper.call("remove_tree", session=directory_name)
                raise
        finally:
            # Victims are already out of `self._sessions`, so they are retired whatever happened
            # above - even when their slot went to a concurrent create() and this one failed.
            for victim in victims:
                try:
                    await self._retire_locked(victim)
                except Exception:
                    LOGGER.exception("Failed to evict session %s", victim.id)
                else:
                    LOGGER.info("Evicted session %s to make room for a new one", victim.id)
                finally:
                    victim.lock.release()
        assert session is not None
        return session

    @contextlib.asynccontextmanager
    async def create_locked(self) -> AsyncGenerator[Session, Any]:
        """Create a session and hold its lock for the body of the `async with`.

        If the body raises, the session is deleted before the exception propagates: a session
        whose setup failed is never handed back to anyone.
        """
        session = await self._create()
        try:
            yield session
        except BaseException:
            try:
                await self._retire_locked(session)
            except Exception:
                LOGGER.exception("Failed to delete session %s after its setup failed", session.id)
            raise
        finally:
            session.last_used = time.monotonic()
            session.lock.release()

    async def _delete(self, session: Session) -> None:
        try:
            await self._fs_helper.call("remove_tree", session=session.directory_name)
        except OSError:
            # Hold the project id back rather than hand a successor an allowance that is already
            # partly spent: the usage is still on disk, and _verify_quota_hard_limit only checks
            # that a hard limit exists, never that usage is zero.
            LOGGER.exception("Failed to remove session directory %s", session.work_directory)
        else:
            self._release_project_id(session.project_id)
        finally:
            # Both the sweep and delete_all iterate a snapshot, so the session may already be gone.
            self._sessions.pop(session.id, None)

    async def _retire_locked(self, session: Session) -> None:
        """Delete a session whose lock the caller holds, and see the deletion through."""
        deletion = asyncio.create_task(self._delete(session))
        try:
            await asyncio.shield(deletion)
        except asyncio.CancelledError:
            # Cancelling the await cannot stop the helper's removal, which carries on in its own
            # process. Keep the session locked until deletion finishes, including during shutdown.
            with contextlib.suppress(Exception):
                await deletion
            raise

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
            # Waiting for the lock is an await, and the sweep or an explicit delete can retire
            # the session during it - the lock is then granted over a directory that is already
            # gone. Re-check after acquiring, not just before.
            if self.get(session_id) is not session:
                raise SessionNotFound(session_id)
            session.last_used = time.monotonic()
            yield session
        finally:
            # Stamped on release too, so idleness - for expiry and eviction alike - counts from
            # the end of the last request, not from the start of a long execution.
            session.last_used = time.monotonic()
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
        for session in list(self._sessions.values()):
            if not await _try_acquire_lock(session.lock):
                continue
            try:
                if self._sessions.get(session.id) is not session:
                    continue
                if now - session.last_used <= SESSION_INACTIVITY_TIMEOUT_SECONDS:
                    continue
                await self._retire_locked(session)
            except Exception as e:
                LOGGER.exception("Failed to sweep expired session %s", session.id, exc_info=e)
            else:
                LOGGER.info("Swept expired session %s", session.id)
            finally:
                session.lock.release()

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
            # Keep going: this runs at shutdown, so one failing session must not abandon the
            # rest and leak their directories.
            try:
                await self._delete(session)
            except Exception:
                LOGGER.exception("Failed to delete session %s during shutdown", session.id)
