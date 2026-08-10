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
    "java": ("/executors/java.sh",),
}


class ExecutionResourceLimitReached(Exception):
    pass


class ExecutionResult(NamedTuple):
    output: str
    output_truncated: bool
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


def _hash_file(path: str) -> tuple[int, bytes]:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", path)
        sha256 = hashlib.sha256()
        with os.fdopen(fd, "rb") as f:
            fd = -1
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return file_stat.st_size, sha256.digest()
    finally:
        if fd != -1:
            os.close(fd)


def read_max_and_close(master_fd: int, slave_fd: int, stop_evt: threading.Event, max_size: int = MAX_OUTPUT_SIZE) -> tuple[bytes, bool]:
    try:
        output = io.BytesIO()
        output_truncated = False
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
            if len(chunk) > remaining_size:
                output_truncated = True
        return output.getvalue(), output_truncated
    finally:
        _close_noerror(master_fd)
        _close_noerror(slave_fd)


async def _run_docker_check(*arguments: str) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError("Docker is not available") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Docker environment check timed out") from None
    if process.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "Docker environment check failed")
    return stdout


async def check_docker_environment() -> None:
    if os.getuid() == 0:
        raise RuntimeError("CodeExecutorAPI must not run as root")
    await _run_docker_check("info")
    await _run_docker_check("image", "inspect", DOCKER_IMAGE)


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
            for file in files:
                full_path = os.path.join(directory, file)
                try:
                    file_size, file_hash = _hash_file(full_path)
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
        output_truncated = False
        try:
            master_fd, slave_fd = pty.openpty()
            read_task = asyncio.create_task(asyncio.to_thread(read_max_and_close, master_fd, slave_fd, evt))
            command = COMMANDS[self.language]
            current_niceness = min(19, os.nice(0) + CONTAINER_RELATIVE_NICENESS)
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
                "--network=none",
                "--tmpfs", f"/tmp:rw,nosuid,nodev,exec,size={CONTAINER_TMPFS_SIZE}",
                "--interactive", "--tty", "--rm",
                "--label", "code_executor_api.managed=true",
                "--user", f"{os.getuid()}:{os.getgid()}",
                f"--memory={MAX_MEMORY}",
                f"--memory-swap={MAX_MEMORY}",
                f"--cpus={MAX_CPU_CORES}",
                "--volume", f"{self.host_work_directory}:/app/",
                "--name", self.container_name,
                DOCKER_IMAGE,
                "nice", "-n", str(current_niceness),
                *command, self.code,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            )
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=EXECUTION_TIMEOUT)
            except TimeoutError:
                timed_out = True
        finally:
            if process is not None and (process.returncode is None or process.returncode != 0):
                removed = await _remove_container(self.container_name)
                if not removed and process.returncode is None:
                    LOGGER.error("Failed to remove container %s", self.container_name)
                    with suppress(ProcessLookupError):
                        process.kill()
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
            evt.set()
            if read_task is not None:
                output, output_truncated = await read_task
            else:
                if master_fd is not None:
                    _close_noerror(master_fd)
                if slave_fd is not None:
                    _close_noerror(slave_fd)
        execution_time = time.perf_counter() - start

        return ExecutionResult(
            output=output.decode("utf-8", "surrogateescape"), output_truncated=output_truncated,
            return_code=return_code, execution_time=execution_time, timed_out=timed_out,
        )

    def get_attachments(self) -> tuple[list[ResultAttachment], list[str]]:
        attachments = []
        seen_sub_paths = set()
        session_entries = 0
        session_size = 0
        cache_directory = os.path.join(self.host_work_directory, ".cache")
        for directory, directories, files in os.walk(self.host_work_directory):
            session_entries += len(directories) + len(files)
            if session_entries > MAX_SESSION_FILES:
                raise ExecutionResourceLimitReached("Session contains too many files")
            in_cache = directory == cache_directory or directory.startswith(cache_directory + os.sep)

            for file in files:
                full_path = os.path.join(directory, file)

                try:
                    file_size, file_hash = _hash_file(full_path)
                except OSError:
                    continue
                session_size += file_size
                if session_size > MAX_SESSION_SIZE:
                    raise ExecutionResourceLimitReached("Session storage limit reached")
                if in_cache:
                    continue

                sub_path = full_path[len(self.host_work_directory) + 1:]
                seen_sub_paths.add(sub_path)
                if (
                        sub_path in self.input_files_hashes and
                        file_size == self.input_files_hashes[sub_path][0] and
                        file_hash == self.input_files_hashes[sub_path][1]
                ):
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
