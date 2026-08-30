import asyncio
import contextlib
import errno
import hashlib
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
from typing import Iterable, NamedTuple

from ..config import (
    CONTAINER_PIDS_LIMIT,
    CONTAINER_RELATIVE_NICENESS,
    CONTAINER_TMPFS_SIZE,
    CONTAINER_ULIMIT_FSIZE,
    CONTAINER_ULIMIT_NOFILE,
    DOCKER_CHECK_TIMEOUT_SECONDS,
    DOCKER_IMAGE,
    EXECUTION_TIMEOUT,
    MAX_CPU_CORES,
    MAX_MEMORY,
    MAX_OUTPUT_SIZE,
    MAX_SESSION_FILES,
    MAX_SESSION_SIZE,
)

LOGGER = logging.getLogger(__name__)

COMMANDS: dict[str, Iterable[str]] = {
    "python": ("python", "-c"),
    "bash": ("bash", "-c"),
    "javascript": ("node", "--input-type=module", "--eval"),
    "c": ("/executors/c.sh",),
    "cpp": ("/executors/cpp.sh",),
    "java": ("/executors/java.sh",),
    "csharp": ("/executors/csharp.sh",),
    "rust": ("/executors/rust.sh",),
}


class ExecutionResourceLimitReached(Exception):
    pass


class ExecutionResult(NamedTuple):
    output: str
    return_code: int
    execution_time: float
    timed_out: bool


class ResultAttachment(NamedTuple):
    sub_path: str
    absolute_path: str


class CodeExecutionResult(NamedTuple):
    execution_result: ExecutionResult
    attachments: list[ResultAttachment]
    deleted_files: list[str]


def _close_noerror(fd: int):
    try:
        os.close(fd)
    except OSError:
        pass


@contextlib.contextmanager
def _open_regular_file(path: str, mode: int | None = None):
    """Open a file for reading, refusing to follow symlinks, as a plain binary file object.

    The builtin `open()`/`pathlib` cannot refuse to follow a symlink, so a raw
    `O_NOFOLLOW` open is unavoidable here; it is confined to this one helper, which
    yields a normal file object (plus its size) once the target is verified regular.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", path)
        if mode is not None:
            os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        yield f, file_stat.st_size


def _hash(f) -> bytes:
    sha256 = hashlib.sha256()
    for chunk in iter(lambda: f.read(4096), b""):
        sha256.update(chunk)
    return sha256.digest()


def _hash_file(path: str, mode: int | None = None) -> tuple[int, bytes]:
    with _open_regular_file(path, mode) as (f, size):
        return size, _hash(f)


def _chmod_directory(path: str) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, 0o777)
    finally:
        os.close(fd)


def read_max_and_close(master_fd: int, slave_fd: int, stop_evt: threading.Event, max_size: int = MAX_OUTPUT_SIZE) -> bytes:
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
            "docker", "rm", "--force", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return False
    return return_code == 0


class ExecutionEnvironment:
    __slots__ = ("language", "code", "container_name", "host_work_directory", "input_files_hashes")

    def __init__(self, langage: str, code, host_work_directory: str):
        if langage not in COMMANDS:
            raise ValueError(f"Unsupported language: {langage}")
        self.language = langage
        self.code = code

        self.container_name = f"ce_{secrets.token_urlsafe(16)}"
        self.host_work_directory = host_work_directory

        self.input_files_hashes: dict[str, tuple[int, bytes]] = {}

        for directory, directories, files in os.walk(self.host_work_directory):
            if directory == self.host_work_directory:
                with contextlib.suppress(ValueError):
                    directories.remove(".cache")
            try:
                _chmod_directory(directory)
            except OSError:
                directories.clear()
                continue
            for file in files:
                full_path = os.path.join(directory, file)
                try:
                    file_size, file_hash = _hash_file(full_path, 0o666)
                except OSError:
                    continue
                sub_path = full_path[len(self.host_work_directory) + 1:]
                self.input_files_hashes[sub_path] = (file_size, file_hash)

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
                "docker", "run",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit", str(CONTAINER_PIDS_LIMIT),
                "--ulimit", f"nofile={CONTAINER_ULIMIT_NOFILE}:{CONTAINER_ULIMIT_NOFILE}",
                "--ulimit", f"fsize={CONTAINER_ULIMIT_FSIZE}:{CONTAINER_ULIMIT_FSIZE}",
                "--cgroupns=private",
                "--ipc=none",
                "--net=bridge",
                "--tmpfs", f"/tmp:rw,nosuid,nodev,exec,size={CONTAINER_TMPFS_SIZE}",
                "--interactive", "--tty", "--rm",
                "--label", "code_executor_api.managed=true",
                f"--memory={MAX_MEMORY}",
                f"--memory-swap={MAX_MEMORY}",
                f"--cpus={MAX_CPU_CORES}",
                "--volume", f"{self.host_work_directory}:/app/",
                "--name", self.container_name,
                DOCKER_IMAGE,
                "nice", "-n", str(current_niceness + CONTAINER_RELATIVE_NICENESS),
                *command, self.code,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            )
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=EXECUTION_TIMEOUT)
            except TimeoutError:
                timed_out = True
        finally:
            if process is not None and process.returncode is None:
                # `docker run --rm` already cleans up on a normal exit; only a still-running
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

    def get_attachments(self) -> tuple[list[ResultAttachment], list[str]]:
        attachments = []
        seen_sub_paths = set()
        session_entries = 0
        session_size = 0
        for directory, directories, files in os.walk(self.host_work_directory):
            if directory == self.host_work_directory:
                with contextlib.suppress(ValueError):
                    directories.remove(".cache")
            session_entries += len(directories) + len(files)
            if session_entries > MAX_SESSION_FILES:
                raise ExecutionResourceLimitReached("Session contains too many files")

            for file in files:
                full_path = os.path.join(directory, file)

                try:
                    with _open_regular_file(full_path) as (f, file_size):
                        session_size += file_size
                        sub_path = full_path[len(self.host_work_directory) + 1:]
                        seen_sub_paths.add(sub_path)

                        previous = self.input_files_hashes.get(sub_path)
                        unchanged = (
                                previous is not None and
                                previous[0] == file_size and
                                _hash(f) == previous[1]
                        )
                except OSError:
                    continue

                if session_size > MAX_SESSION_SIZE:
                    raise ExecutionResourceLimitReached("Session storage limit reached")
                if unchanged:
                    continue  # Skip input attachments that match the original content

                attachments.append(ResultAttachment(sub_path=sub_path, absolute_path=full_path))

        deleted_files = list(self.input_files_hashes.keys() - seen_sub_paths)
        return attachments, deleted_files


async def run_code_async(work_directory: str, language: str, code: str) -> CodeExecutionResult:
    environment: ExecutionEnvironment = await asyncio.to_thread(ExecutionEnvironment, language, code, work_directory)
    execution_result = await environment.run_container()
    try:
        result_attachments, deleted_files = await asyncio.wait_for(
            asyncio.to_thread(environment.get_attachments),
            timeout=EXECUTION_TIMEOUT,
        )
    except TimeoutError:
        raise ExecutionResourceLimitReached("Execution result scan timed out") from None
    return CodeExecutionResult(execution_result=execution_result, attachments=result_attachments, deleted_files=deleted_files)
