"""API-side client for the session file helper (`fs_helper.py`).

Executed code runs as a subordinate uid that the API's service account cannot act for, so every
session-tree operation goes through a helper running under `podman unshare`. The helper hands
files back as open descriptors, which the caller then reads or writes exactly as it would one it
had opened itself.

The helper speaks over a Unix stream socket (its stdin). asyncio has no `recvmsg` with
`SCM_RIGHTS`, so a reader thread does blocking receives and resolves the callers' futures on the
event loop. Requests are a few hundred bytes at most and are sent straight from the event loop.
"""

import asyncio
import collections
import contextlib
import dataclasses
import errno
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from .config import CONTAINER_USER_ID, SESSION_ROOT_DIRECTORY

LOGGER = logging.getLogger(__name__)

_HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fs_helper.py")
# Only what the helper uses survives in its bounding set: chown and fchown to the container
# user, reading, walking and removing trees that user locked down, and removing entries from
# sticky directories it owns.
_SETPRIV_ARGUMENTS = ("--bounding-set=-all,+chown,+dac_override,+fowner", "--no-new-privs")
# How long the first `ping` may take. On a cold boot `podman unshare` may first have to start
# podman's pause process and set up the user namespace, which takes far longer than a warm start.
_STARTUP_TIMEOUT_SECONDS = 60.0
_RESTART_BACKOFF_SECONDS = 1.0
_STOP_TIMEOUT_SECONDS = 5.0
_RECEIVE_SIZE = 65536
_MAX_FDS_PER_RECEIVE = 4


@dataclasses.dataclass
class _PendingCall:
    future: asyncio.Future
    op: str
    args: dict[str, Any]


class _Connection:
    """One running helper process: its socket, its reader thread, and the calls awaiting it.

    A helper that dies is never revived; the client starts a new `_Connection` instead, so a
    reader thread can only ever resolve calls sent to its own process.
    """

    def __init__(self, process: subprocess.Popen, sock: socket.socket, loop: asyncio.AbstractEventLoop, client: "FsHelper"):
        self.process = process
        self.socket = sock
        self.loop = loop
        self.client = client
        # Every request until its reply arrives, even once its future is done (cancelled caller,
        # dead connection): a late reply still needs its op and args to be undone.
        self.pending: dict[int, _PendingCall] = {}
        self.alive = True
        self._next_id = 0
        self.reader = threading.Thread(target=self._read_loop, name="fs-helper-reader", daemon=True)
        self.reader.start()

    def send(self, op: str, args: dict[str, Any]) -> asyncio.Future:
        """Send a request and return the future its reply resolves. Event loop thread only."""
        self._next_id += 1
        request_id = self._next_id
        body = json.dumps({"id": request_id, "op": op, "args": args}).encode()
        future = self.loop.create_future()
        self.pending[request_id] = _PendingCall(future, op, args)
        try:
            self.socket.sendall(len(body).to_bytes(4, "big") + body)
        except OSError as exc:
            del self.pending[request_id]
            self.mark_dead(f"send failed: {exc}")
            raise OSError(errno.EIO, "The file helper is not running") from exc
        return future

    def _read_loop(self) -> None:
        buffer = bytearray()
        # Each descriptor arrives with the first byte of its message, so in message order, and
        # always before that message's header has been fully read.
        received_fds: collections.deque[int] = collections.deque()
        reason = "connection closed"
        try:
            while True:
                try:
                    data, fds, flags, _ = socket.recv_fds(self.socket, _RECEIVE_SIZE, _MAX_FDS_PER_RECEIVE)
                except OSError as exc:
                    reason = f"receive failed: {exc}"
                    break
                received_fds.extend(fds)
                if flags & socket.MSG_CTRUNC:
                    # The kernel dropped descriptors, so the fd queue no longer matches the
                    # replies; nothing after this point can be trusted.
                    reason = "descriptors were truncated"
                    break
                if not data:
                    break
                buffer += data
                while len(buffer) >= 4:
                    length = int.from_bytes(buffer[:4], "big")
                    if len(buffer) < 4 + length:
                        break
                    message = json.loads(buffer[4:4 + length])
                    del buffer[:4 + length]
                    fd = None
                    if message.get("fd"):
                        if not received_fds:
                            raise RuntimeError("file helper reply announced a descriptor that never arrived")
                        fd = received_fds.popleft()
                    try:
                        self.loop.call_soon_threadsafe(self.client._resolve, self, message, fd)
                    except RuntimeError:
                        # The event loop is closed: nobody is left to hand the descriptor to.
                        if fd is not None:
                            _close_quietly(fd)
        except Exception:
            LOGGER.exception("File helper reader failed")
            reason = "reader failed"
        finally:
            for fd in received_fds:
                _close_quietly(fd)
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.client._on_connection_lost, self, reason)

    def mark_dead(self, reason: str) -> None:
        """Fail every pending call and stop accepting new ones. Event loop thread only.

        The calls stay in `pending`: a reply the reader still receives afterwards is then undone
        by `FsHelper._resolve`. An operation the helper completed without its reply ever being
        received (the helper died, or the API end closed first) is not undone.
        """
        if not self.alive:
            return
        self.alive = False
        if not self.client._stopping:
            LOGGER.error("File helper (pid %s) lost: %s", self.process.pid, reason)
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        for call in self.pending.values():
            if not call.future.done():
                call.future.set_exception(OSError(errno.EIO, "The file helper exited"))

    def close(self, timeout: float) -> None:
        """Close the socket, reap the process and the reader thread. Blocking: run in a thread."""
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        # EOF on its stdin is the helper's signal to exit.
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOGGER.error("File helper (pid %s) did not exit, killing it", self.process.pid)
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=timeout)
        self.socket.close()


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class FsHelper:
    """Client for the file helper: starts it, restarts it after it dies, and forwards calls.

    `call` returns `(result, fd)`. The caller owns `fd` and must close it. A helper error
    becomes `OSError(errno, strerror, sub_path)`, so the errno-specific subclasses
    (`FileNotFoundError`, `IsADirectoryError`, ...) reach the existing handlers unchanged. A
    helper that dies fails its in-flight calls with EIO; the next call starts a new one.
    """

    def __init__(self) -> None:
        # Resolved, because the helper opens it with `O_NOFOLLOW`, and so that the session paths
        # given to podman and xfs_quota are the real ones.
        self._session_root = os.path.realpath(SESSION_ROOT_DIRECTORY or tempfile.gettempdir())
        self._connection: _Connection | None = None
        self._start_lock = asyncio.Lock()
        self._last_start = 0.0
        self._stopping = False
        # Strong references to fire-and-forget cleanup tasks, which the loop only holds weakly.
        self._cleanup_tasks: set[asyncio.Task] = set()

    @property
    def session_root(self) -> str:
        return self._session_root

    def _command(self) -> list[str]:
        command = ["podman", "unshare", "--"]
        setpriv = shutil.which("setpriv")
        if setpriv is not None:
            command += [setpriv, *_SETPRIV_ARGUMENTS]
        else:
            LOGGER.warning("setpriv not found: the file helper keeps every capability of the namespace")
        return [*command, sys.executable, "-I", "-S", _HELPER_PATH, self._session_root, str(CONTAINER_USER_ID)]

    def _spawn(self, loop: asyncio.AbstractEventLoop) -> _Connection:
        api_socket, helper_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # stdout is kept off the socket, which carries only framed replies. stderr is
            # inherited so the helper's own errors reach the journal. cwd is `/` for the same
            # reason podman runs from there in podman_executor.py.
            process = subprocess.Popen(
                self._command(),
                stdin=helper_socket.fileno(),
                stdout=subprocess.DEVNULL,
                cwd="/",
                close_fds=True,
            )
        except BaseException:
            api_socket.close()
            raise
        finally:
            helper_socket.close()
        return _Connection(process, api_socket, loop, self)

    async def _start_locked(self) -> _Connection:
        wait = self._last_start + _RESTART_BACKOFF_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_start = time.monotonic()
        loop = asyncio.get_running_loop()
        # Shielded, so that a cancelled start still gets hold of the process it spawned.
        spawn = loop.create_task(asyncio.to_thread(self._spawn, loop))
        try:
            connection = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            spawn.add_done_callback(self._close_abandoned_spawn)
            self._track(spawn)
            raise
        self._connection = connection
        try:
            # The helper exits straight away if it cannot open the session root, or if
            # `podman unshare` itself fails; a round trip is what proves it is serving.
            await asyncio.wait_for(connection.send("ping", {}), timeout=_STARTUP_TIMEOUT_SECONDS)
        except BaseException as exc:
            connection.mark_dead(f"startup failed: {exc!r}")
            self._connection = None
            await asyncio.to_thread(connection.close, _STOP_TIMEOUT_SECONDS)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise OSError(errno.EIO, f"The file helper failed to start (exit code {connection.process.returncode})") from exc
        LOGGER.info("File helper started (pid %s) for %s", connection.process.pid, self._session_root)
        return connection

    def _close_abandoned_spawn(self, spawn: asyncio.Task) -> None:
        if spawn.cancelled() or spawn.exception() is not None:
            return
        connection = spawn.result()
        connection.mark_dead("startup cancelled")
        self._track(asyncio.get_running_loop().create_task(asyncio.to_thread(connection.close, _STOP_TIMEOUT_SECONDS)))

    async def start(self) -> None:
        self._stopping = False
        async with self._start_lock:
            if self._connection is None or not self._connection.alive:
                await self._start_locked()

    async def stop(self) -> None:
        self._stopping = True
        async with self._start_lock:
            connection, self._connection = self._connection, None
        if connection is None:
            return
        connection.mark_dead("stopping")
        await asyncio.to_thread(connection.close, _STOP_TIMEOUT_SECONDS)
        LOGGER.info("File helper stopped")

    async def _get_connection(self) -> _Connection:
        connection = self._connection
        if connection is not None and connection.alive:
            return connection
        if self._stopping:
            raise OSError(errno.EIO, "The file helper is stopped")
        async with self._start_lock:
            connection = self._connection
            if connection is not None and connection.alive:
                return connection
            if connection is not None:
                self._connection = None
                await asyncio.to_thread(connection.close, _STOP_TIMEOUT_SECONDS)
            return await self._start_locked()

    async def call(self, op: str, **args) -> tuple[Any, int | None]:
        connection = await self._get_connection()
        future = connection.send(op, args)
        # A caller cancelled before the reply leaves its entry pending: `_resolve` then undoes
        # the late reply. One cancelled after `_resolve` set the result, but before resuming,
        # undoes it here.
        try:
            response, fd = await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled() and future.exception() is None:
                self._undo_orphan(op, args, *future.result())
            raise
        if "errno" in response:
            error_number = response["errno"]
            raise OSError(error_number, os.strerror(error_number), args.get("sub_path"))
        return response.get("ok"), fd

    def _resolve(self, connection: _Connection, message: dict[str, Any], fd: int | None) -> None:
        call = connection.pending.pop(message.get("id"), None)
        if call is None:
            if fd is not None:
                _close_quietly(fd)
            return
        if not call.future.done():
            call.future.set_result((message, fd))
            return
        # Nobody is waiting any more: the caller was cancelled, or the connection was already
        # failed.
        self._undo_orphan(call.op, call.args, message, fd)

    def _undo_orphan(self, op: str, args: dict[str, Any], message: dict[str, Any], fd: int | None) -> None:
        """Undo a reply nobody will consume, so no descriptor, hidden file or session leaks."""
        if fd is not None:
            _close_quietly(fd)
        if "ok" not in message:
            return
        if op == "stage":
            self._spawn_cleanup("discard", session=args.get("session"), sub_path=args.get("sub_path"), temp_name=message["ok"])
        elif op == "create_session":
            self._spawn_cleanup("remove_tree", session=message["ok"])

    def _spawn_cleanup(self, op: str, **args) -> None:
        async def cleanup() -> None:
            try:
                _, fd = await self.call(op, **args)
            except OSError as exc:
                LOGGER.warning("File helper cleanup %s %s failed: %s", op, args, exc)
            else:
                if fd is not None:
                    _close_quietly(fd)

        self._track(asyncio.get_running_loop().create_task(cleanup()))

    def _track(self, task: asyncio.Task) -> None:
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _on_connection_lost(self, connection: _Connection, reason: str) -> None:
        connection.mark_dead(reason)
