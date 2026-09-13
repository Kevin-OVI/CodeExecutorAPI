import asyncio
import errno
import io
import logging
import os
import pty
import secrets
import select
import stat
import threading
import time
from contextlib import suppress
from typing import Iterable, Iterator, NamedTuple

from ..config import (
    CONTAINER_NETWORK,
    CONTAINER_PIDS_LIMIT,
    CONTAINER_RELATIVE_NICENESS,
    CONTAINER_TMPFS_SIZE,
    CONTAINER_ULIMIT_FSIZE,
    CONTAINER_ULIMIT_NOFILE,
    CONTAINER_USER_ID,
    EXECUTION_TIMEOUT,
    MAX_CPU_CORES,
    MAX_MEMORY,
    MAX_OUTPUT_SIZE,
    PODMAN_CHECK_TIMEOUT_SECONDS,
    PODMAN_IMAGE,
)
from ..sessions import Session

LOGGER = logging.getLogger(__name__)

COMMANDS: dict[str, Iterable[str]] = {
    "python": ("python", "-c"),
    "bash": ("bash", "-c"),
    "javascript": ("node", "--eval"),
    "typescript": ("tsx", "--eval"),
    "c": ("/executors/c.sh",),
    "cpp": ("/executors/cpp.sh",),
    "java": ("/executors/java.sh",),
    "csharp": ("/executors/csharp.sh",),
    "rust": ("/executors/rust.sh",),
}


class ExecutionResult(NamedTuple):
    output: str
    return_code: int
    execution_time: float
    timed_out: bool


class CodeExecutionResult(NamedTuple):
    execution_result: ExecutionResult
    changed_files: list[str]
    deleted_files: list[str]


def _close_noerror(fd: int):
    try:
        os.close(fd)
    except OSError:
        pass


def _iter_session_files(work_directory: str) -> Iterator[tuple[str, os.stat_result]]:
    """Yield `(sub_path, lstat)` for every regular file in the session.

    Hidden directories are skipped at any depth: the session directory doubles as the
    container's `$HOME`, so `.cache`, `.local`, `.npm` and friends fill up with package
    manager noise that is never a meaningful execution result.

    Symlinks and other non-regular entries are never yielded, so they can be neither
    reported as results nor followed off the session tree.
    """
    prefix_length = len(work_directory) + 1
    pending = [work_directory]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.startswith("."):
                            pending.append(entry.path)
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(entry_stat.st_mode):
                        yield entry.path[prefix_length:], entry_stat
        except OSError as exc:
            LOGGER.debug("Skipping unreadable session directory %s: %s", directory, exc)
            continue


def _signature(entry_stat: os.stat_result) -> tuple[int, int, int, int]:
    """Cheap change-detection stamp for a regular file.

    A stamp rather than a content hash: one `lstat` instead of a full read. `st_ino`
    catches a delete-and-recreate, and `st_ctime_ns` catches an `mtime` rewound with
    `utimensat` (which bumps `ctime` itself) as well as any metadata-only change.
    """
    return (entry_stat.st_ino, entry_stat.st_size, entry_stat.st_mtime_ns, entry_stat.st_ctime_ns)


def read_max_and_close(master_fd: int, slave_fd: int, stop_evt: threading.Event, max_size: int = MAX_OUTPUT_SIZE) -> bytes:
    """Drain the pty until the container exits, returning at most `max_size` bytes.

    Runs in a worker thread and owns both fds, closing them on the way out.

    Reading continues past `max_size` with the excess discarded: the container writes into a
    fixed-size pty buffer, and a reader that stops wedges it in a write it never returns from.

    `stop_evt` is the exit that fires; the 0.1s `select` timeout is what lets the thread see
    it. This process holds its own `slave_fd` open throughout, so the master never reports EIO
    when the container exits - that branch is a safety net, not the normal path.
    """
    try:
        output = io.BytesIO()
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if not r:
                if stop_evt.is_set():
                    break
                continue
            try:
                chunk = os.read(r[0], 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            remaining_size = max_size - output.tell()
            if remaining_size > 0:
                output.write(chunk[:remaining_size])
        return output.getvalue()
    finally:
        _close_noerror(master_fd)
        _close_noerror(slave_fd)


async def _remove_container(container_name: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "podman", "rm", "--force", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=PODMAN_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return False
    return return_code == 0


class ExecutionEnvironment:
    """One container run against a session, plus the before/after diff of its files.

    Constructing this stamps every file in the session, so it must happen before the container
    starts - `collect_changes` has nothing to compare against otherwise. Neither half takes a
    lock; the caller's session lock is what keeps a concurrent write out.
    """

    __slots__ = ("session", "language", "code", "container_name", "input_files")

    def __init__(self, session: Session, language: str, code: str):
        if language not in COMMANDS:
            raise ValueError(f"Unsupported language: {language}")
        self.session = session
        self.language = language
        self.code = code

        self.container_name = f"ce_{secrets.token_urlsafe(16)}"

        self.input_files: dict[str, tuple[int, int, int, int]] = {
            sub_path: _signature(entry_stat)
            for sub_path, entry_stat in _iter_session_files(session.work_directory)
        }

    async def run_container(self) -> ExecutionResult:
        return_code = -1
        timed_out = False
        evt = threading.Event()
        start = time.perf_counter()
        master_fd: int | None = None
        slave_fd: int | None = None
        read_task = None
        process = None
        output = b""
        try:
            master_fd, slave_fd = pty.openpty()
            read_task = asyncio.create_task(asyncio.to_thread(read_max_and_close, master_fd, slave_fd, evt))
            command = COMMANDS[self.language]
            current_niceness = os.nice(0)
            process = await asyncio.create_subprocess_exec(
                "podman", "run",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit", str(CONTAINER_PIDS_LIMIT),
                "--ulimit", f"nofile={CONTAINER_ULIMIT_NOFILE}:{CONTAINER_ULIMIT_NOFILE}",
                "--ulimit", f"fsize={CONTAINER_ULIMIT_FSIZE}:{CONTAINER_ULIMIT_FSIZE}",
                "--cgroupns=private",
                # Map the container's `appuser` onto the host service account, so session files
                # have a single owner on both sides of the bind mount. Without it the two uids
                # differ under rootless Podman and neither side can fully manage the other's
                # files. The explicit uid/gid is required: a bare `keep-id` maps the host user to
                # the *same* id in the container, which only lines up if the service account
                # happens to share CONTAINER_USER_ID.
                f"--userns=keep-id:uid={CONTAINER_USER_ID},gid={CONTAINER_USER_ID}",
                "--ipc=none",
                f"--net={CONTAINER_NETWORK}",
                "--sysctl", f"net.ipv4.ping_group_range={CONTAINER_USER_ID} {CONTAINER_USER_ID}",
                "--tmpfs", f"/tmp:rw,nosuid,nodev,exec,size={CONTAINER_TMPFS_SIZE}",
                "--interactive", "--tty", "--rm",
                "--label", "code_executor_api.managed=true",
                f"--memory={MAX_MEMORY}",
                f"--memory-swap={MAX_MEMORY}",
                f"--cpus={MAX_CPU_CORES}",
                "--volume", f"{self.session.work_directory}:/app/",
                "--name", self.container_name,
                PODMAN_IMAGE,
                "nice", "-n", str(current_niceness + CONTAINER_RELATIVE_NICENESS),
                *command, self.code,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                # conmon records a cgroup OOM kill by creating an empty marker file named `oom`
                # in its working directory, which it inherits from podman, which inherits it from
                # this process - so a run that exhausts MAX_MEMORY would otherwise litter whatever
                # directory the API was started from. Rooting podman at `/` sends the marker
                # somewhere unwritable, where conmon quietly gives up on it. Nothing here reads it:
                # an OOM kill is already visible as the container's non-zero return code, and the
                # only consumer of the marker is `podman inspect`'s OOMKilled field, which this
                # `--rm` run never queries. Every path passed above must therefore be absolute.
                cwd="/",
            )
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=EXECUTION_TIMEOUT)
            except TimeoutError:
                timed_out = True
        finally:
            if process is not None and process.returncode is None:
                # `podman run --rm` already cleans up on a normal exit; only a still-running
                # (timed-out) container needs to be force-removed here.
                removed = await _remove_container(self.container_name)
                if not removed:
                    LOGGER.error("Failed to remove container %s", self.container_name)
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
            evt.set()
            if read_task is not None:
                output = await read_task
            else:
                if master_fd is not None:
                    _close_noerror(master_fd)
                if slave_fd is not None:
                    _close_noerror(slave_fd)
        execution_time = time.perf_counter() - start

        return ExecutionResult(
            output=output.decode("utf-8", "surrogateescape").rstrip(),
            return_code=return_code, execution_time=execution_time, timed_out=timed_out,
        )

    def collect_changes(self) -> tuple[list[str], list[str]]:
        """Return `(changed_files, deleted_files)` for the finished run, both complete.

        The whole session is walked - it is bounded by the session's inode quota - and every
        difference is reported. Deciding how many of these fit in a response is the caller's
        job, since the caller is what opens and serialises them.

        Both lists are sorted, so a caller that has to truncate cuts deterministically.
        """
        changed_files = []
        current_files: set[str] = set()
        for sub_path, entry_stat in _iter_session_files(self.session.work_directory):
            current_files.add(sub_path)
            if self.input_files.get(sub_path) != _signature(entry_stat):
                changed_files.append(sub_path)

        changed_files.sort()
        return changed_files, sorted(self.input_files.keys() - current_files)


async def run_code_async(session: Session, language: str, code: str) -> CodeExecutionResult:
    environment: ExecutionEnvironment = await asyncio.to_thread(ExecutionEnvironment, session, language, code)
    execution_result = await environment.run_container()
    changed_files, deleted_files = await asyncio.to_thread(environment.collect_changes)
    return CodeExecutionResult(
        execution_result=execution_result,
        changed_files=changed_files,
        deleted_files=deleted_files,
    )
