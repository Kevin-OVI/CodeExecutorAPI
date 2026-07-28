import asyncio
import contextlib
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
    CONTAINER_ULIMIT_NPROC,
    DOCKER_IMAGE,
    EXECUTION_TIMEOUT,
    MAX_CPU_CORES,
    MAX_MEMORY,
    MAX_OUTPUT_SIZE,
)

LOGGER = logging.getLogger(__name__)

COMMANDS: dict[str, Iterable[str]] = {
    "python": ("python", "-c"),
    "bash": ("bash", "-c"),
    "javascript": ("node", "--input-type=module", "--eval"),
    "c": ("/executors/c.sh",),
    "java": ("/executors/java.sh",),
}


class _CalledProcessError(Exception):
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


def _hash_file(path: str) -> bytes:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    return sha256.digest()


def read_max_and_close(master_fd: int, slave_fd: int, stop_evt: threading.Event, max_size: int = MAX_OUTPUT_SIZE) -> bytes:
    try:
        output = io.BytesIO()
        read_size = 0
        while read_size < max_size and not stop_evt.is_set():
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if not r:
                continue
            chunk = os.read(r[0], min(4096, max_size - read_size))
            read_size += len(chunk)
            output.write(chunk)
        stop_evt.wait()
        return output.getvalue()
    finally:
        _close_noerror(master_fd)
        _close_noerror(slave_fd)


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

        for directory, _, files in os.walk(self.host_work_directory):
            os.chmod(directory, 0o777)
            for file in files:
                full_path = os.path.join(directory, file)
                os.chmod(full_path, 0o666)

                try:
                    file_stat = os.lstat(full_path)
                except OSError:
                    continue

                # Only consider regular files to avoid symlink/device abuse.
                if not stat.S_ISREG(file_stat.st_mode):
                    continue

                sub_path = full_path[len(self.host_work_directory) + 1:]
                self.input_files_hashes[sub_path] = (file_stat.st_size, _hash_file(full_path))

    async def run_container(self) -> ExecutionResult:
        return_code = -1
        timed_out = False
        master_fd, slave_fd = pty.openpty()
        evt = threading.Event()
        read_task = asyncio.create_task(asyncio.to_thread(read_max_and_close, master_fd, slave_fd, evt))
        start = time.perf_counter()

        command = COMMANDS[self.language]
        current_niceness = os.nice(0)
        process = await asyncio.create_subprocess_exec(
            "docker", "run",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit", str(CONTAINER_PIDS_LIMIT),
            "--ulimit", f"nofile={CONTAINER_ULIMIT_NOFILE}:{CONTAINER_ULIMIT_NOFILE}",
            "--ulimit", f"nproc={CONTAINER_ULIMIT_NPROC}:{CONTAINER_ULIMIT_NPROC}",
            "--ulimit", f"fsize={CONTAINER_ULIMIT_FSIZE}:{CONTAINER_ULIMIT_FSIZE}",
            "--cgroupns=private",
            "--ipc=none",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,exec,size={CONTAINER_TMPFS_SIZE}",
            "--interactive", "--tty", "--rm",
            f"--memory={MAX_MEMORY}",
            f"--memory-swap={MAX_MEMORY}",
            f"--cpus={MAX_CPU_CORES}",
            "--volume", f"{self.host_work_directory}:/app/",
            f"--net=bridge",
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
            try:
                killer_process = await asyncio.create_subprocess_exec("docker", "rm", "--force", self.container_name)
                killer_return_code = await asyncio.wait_for(killer_process.wait(), timeout=5)
                if killer_return_code != 0:
                    raise _CalledProcessError(f"Killer process returned code {killer_return_code}")
            except (_CalledProcessError, TimeoutError) as e:
                LOGGER.exception("Failed to remove container %s after timeout!", self.container_name, exc_info=e)
                with suppress(ProcessLookupError):
                    process.kill()  # Last resort to interrupt the process by killing the container process
        finally:
            evt.set()
            if process.returncode is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)

        output = await read_task
        execution_time = time.perf_counter() - start

        return ExecutionResult(
            output=output.decode("utf-8", "surrogateescape").rstrip(),
            return_code=return_code, execution_time=execution_time, timed_out=timed_out,
        )

    async def get_attachments(self) -> tuple[list[ResultAttachment], list[str]]:
        attachments = []
        seen_sub_paths = []
        for directory, directories, files in os.walk(self.host_work_directory):
            if directory == self.host_work_directory:
                with contextlib.suppress(ValueError):
                    directories.remove(".cache")

            for file in files:
                full_path = os.path.join(directory, file)

                try:
                    file_stat = os.lstat(full_path)
                except OSError:
                    continue

                # Only return regular files to avoid symlink/device abuse.
                if not stat.S_ISREG(file_stat.st_mode):
                    continue

                sub_path = full_path[len(self.host_work_directory) + 1:]
                seen_sub_paths.append(sub_path)
                if (
                        sub_path in self.input_files_hashes and
                        file_stat.st_size == self.input_files_hashes[sub_path][0] and
                        await asyncio.to_thread(_hash_file, full_path) == self.input_files_hashes[sub_path][1]
                ):
                    continue  # Skip input attachments that match the original content

                attachments.append(ResultAttachment(sub_path=sub_path, absolute_path=full_path))

        deleted_files = list(self.input_files_hashes.keys() - seen_sub_paths)
        return attachments, deleted_files


async def run_code_async(work_directory: str, language: str, code: str) -> CodeExecutionResult:
    environment: ExecutionEnvironment = await asyncio.to_thread(ExecutionEnvironment, language, code, work_directory)
    execution_result = await environment.run_container()
    result_attachments, deleted_files = await environment.get_attachments()
    return CodeExecutionResult(execution_result=execution_result, attachments=result_attachments, deleted_files=deleted_files)
