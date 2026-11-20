"""File helper for session trees, run inside podman's rootless user namespace.

Executed code runs as CONTAINER_USER_ID inside the container, which is a subordinate uid on the
host: the API's service account can neither read nor delete what that code creates, and the code
cannot use what the API would create directly. This helper runs under `podman unshare`, where it
holds `CAP_CHOWN`, `CAP_DAC_OVERRIDE` and `CAP_FOWNER` over both ids, and does the session-tree
operations that need them on the API's behalf. Files are handed back as open descriptors
(`SCM_RIGHTS`), so the API keeps streaming them itself.

Started as `fs_helper.py <session root> <container uid>` with a Unix stream socket as stdin, by
`code_executor_api.fs_client.FsHelper`. It imports nothing but the standard library: it runs with
namespace privileges, so nothing from the API package - or from the API's environment or its
venv's site-packages, hence `python -I -S` - may run in it.

Protocol: every message is a 4-byte big-endian length followed by a JSON body. Requests are
`{"id", "op", "args"}`; responses are `{"id", "ok": result, "fd": bool}` or `{"id", "errno": N}`,
with at most one descriptor attached to the first byte of a response whose `fd` is true.

Confinement: the session root is opened once, and every operation acts relative to that
descriptor. Executed code owns everything below a session directory and can swap any component
for a symlink at any moment, so paths are walked one component at a time with `O_NOFOLLOW`, and
each syscall acts on the parent directory's descriptor, never on a reassembled path.
"""

import contextlib
import errno
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
import traceback
from typing import Any, Callable

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SESSION_PREFIX = "code_executor_session_"
_SESSION_NAME = re.compile(re.escape(_SESSION_PREFIX) + r"[^/\x00]+")
_TEMPORARY_PREFIX = ".code_executor_upload_"
_TEMPORARY_NAME = re.compile(re.escape(_TEMPORARY_PREFIX) + r"[A-Za-z0-9_-]+")
# The alphabet `tempfile.mkdtemp` draws its names from.
_SESSION_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_"

_root_fd: int = -1
_container_id: int = -1
_socket: socket.socket
_send_lock = threading.Lock()


class _Reject(Exception):
    """A request that fails validation. Answered as ENOENT: an escape attempt need not be
    distinguishable from a typo."""


def _log(message: str) -> None:
    print(f"fs_helper: {message}", file=sys.stderr, flush=True)


def _session_name(args: dict[str, Any]) -> str:
    session = args.get("session")
    if not isinstance(session, str) or _SESSION_NAME.fullmatch(session) is None:
        raise _Reject
    return session


def _path_parts(args: dict[str, Any], allow_empty: bool = False) -> list[str]:
    sub_path = args.get("sub_path")
    if not isinstance(sub_path, str):
        raise _Reject
    if sub_path == "" and allow_empty:
        return []
    parts = sub_path.split("/")
    for part in parts:
        if part in ("", ".", "..") or "\x00" in part:
            raise _Reject
    return parts


def _temporary_name(args: dict[str, Any]) -> str:
    temporary_name = args.get("temp_name")
    if not isinstance(temporary_name, str) or _TEMPORARY_NAME.fullmatch(temporary_name) is None:
        raise _Reject
    return temporary_name


def _open_session(args: dict[str, Any]) -> int:
    try:
        return os.open(_session_name(args), _DIRECTORY_FLAGS, dir_fd=_root_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _Reject from None
        raise


@contextlib.contextmanager
def _open_parent(args: dict[str, Any], create_directories: bool = False, allow_empty: bool = False):
    """Open the directory holding `sub_path`, yielding `(fd, final_name)`.

    `final_name` is None for an empty `sub_path` (only with `allow_empty`), in which case `fd`
    is the session directory itself. A symlinked or non-directory component is reported as
    ENOENT. Directories created on the way belong to the container user, so executed code can
    write into them.
    """
    parts = _path_parts(args, allow_empty)
    parent_fd = _open_session(args)
    try:
        for part in parts[:-1]:
            created = False
            if create_directories:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _Reject from None
                raise
            os.close(parent_fd)
            parent_fd = next_fd
            if created:
                # Through the descriptor rather than the name: executed code may already have
                # swapped the new directory for something else, and this must not follow it.
                os.fchown(parent_fd, _container_id, _container_id)
        yield parent_fd, (parts[-1] if parts else None)
    finally:
        os.close(parent_fd)


def _create_session(args: dict[str, Any]) -> tuple[Any, int | None]:
    """Create a session directory, mode 0700, still owned by the namespace root.

    The namespace root is the API's service account on the host, which is what lets the API tag
    the directory with its XFS project quota (`xfs_quota` can only do that on a directory its
    caller owns). `hand_over` then gives it to the container user.
    """
    while True:
        name = _SESSION_PREFIX + "".join(secrets.choice(_SESSION_NAME_ALPHABET) for _ in range(8))
        try:
            os.mkdir(name, 0o700, dir_fd=_root_fd)
        except FileExistsError:
            continue
        break
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=_root_fd)
        try:
            # mkdir's mode went through the umask; this makes it exact.
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=_root_fd)
        raise
    return name, None


def _hand_over(args: dict[str, Any]) -> tuple[Any, int | None]:
    """Give a session directory itself, not its contents, to the container user."""
    session_fd = _open_session(args)
    try:
        os.fchown(session_fd, _container_id, _container_id)
    finally:
        os.close(session_fd)
    return None, None


def _open_read(args: dict[str, Any]) -> tuple[Any, int | None]:
    with _open_parent(args) as (parent_fd, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except OSError as exc:
            # None of these has content to serve: a looping or dangling symlink (ELOOP), a
            # socket (ENXIO), a device node with no driver behind it (ENODEV). Errnos that mean
            # the server is in trouble (EMFILE, ENOMEM) keep their own.
            if exc.errno in (errno.ELOOP, errno.ENXIO, errno.ENODEV, errno.ENOTDIR):
                raise _Reject from None
            raise
    return None, fd


def _stage(args: dict[str, Any]) -> tuple[Any, int | None]:
    with _open_parent(args, create_directories=True) as (parent_fd, _):
        temporary_name = f"{_TEMPORARY_PREFIX}{secrets.token_urlsafe(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchown(fd, _container_id, _container_id)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_fd)
            raise
    return temporary_name, fd


def _commit(args: dict[str, Any]) -> tuple[Any, int | None]:
    temporary_name = _temporary_name(args)
    with _open_parent(args) as (parent_fd, name):
        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    return None, None


def _discard(args: dict[str, Any]) -> tuple[Any, int | None]:
    temporary_name = _temporary_name(args)
    with _open_parent(args) as (parent_fd, _):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
    return None, None


def _delete(args: dict[str, Any]) -> tuple[Any, int | None]:
    with _open_parent(args) as (parent_fd, name):
        if stat.S_ISDIR(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR))
        os.unlink(name, dir_fd=parent_fd)
    return None, None


def _entry_type(st_mode: int) -> str:
    if stat.S_ISREG(st_mode):
        return "file"
    if stat.S_ISDIR(st_mode):
        return "directory"
    if stat.S_ISLNK(st_mode):
        return "symlink"
    return "other"


def _list(args: dict[str, Any]) -> tuple[Any, int | None]:
    """List one directory level. Hidden entries and symlinks are reported too, so a caller can
    always discover what a session actually holds."""
    with _open_parent(args, allow_empty=True) as (parent_fd, name):
        if name is None:
            fd = os.dup(parent_fd)
        else:
            try:
                fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)) from None
                raise
    try:
        entries = []
        for entry_name in os.listdir(fd):
            try:
                entry_stat = os.stat(entry_name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": entry_name,
                "type": _entry_type(entry_stat.st_mode),
                "size": entry_stat.st_size,
                "modified_at": entry_stat.st_mtime,
            })
    finally:
        os.close(fd)
    entries.sort(key=lambda entry: entry["name"])
    return entries, None


def _snapshot(args: dict[str, Any]) -> tuple[Any, int | None]:
    """Stamp every regular file in the session as `[sub_path, ino, size, mtime_ns, ctime_ns]`.

    Hidden directories are skipped at any depth: the session directory doubles as the
    container's `$HOME`, so `.cache`, `.local`, `.npm` and friends fill up with package manager
    noise that is never a meaningful execution result. Symlinks and other non-regular entries
    are never reported nor followed: every directory is opened with `O_NOFOLLOW` relative to its
    parent's descriptor. One descriptor per level is kept open, and a directory that cannot be
    opened (removed mid-walk, swapped for a symlink) is skipped.
    """
    rows = []
    session_fd = _open_session(args)
    # Each level: its descriptor, its sub_path prefix, and the subdirectories still to visit.
    stack: list[tuple[int, str, list[str]]] = []
    try:
        directory_fd, prefix = session_fd, ""
        while True:
            subdirectories = []
            try:
                names = os.listdir(directory_fd)
            except OSError:
                names = []
            for name in names:
                try:
                    entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(entry_stat.st_mode):
                    rows.append([
                        prefix + name,
                        entry_stat.st_ino,
                        entry_stat.st_size,
                        entry_stat.st_mtime_ns,
                        entry_stat.st_ctime_ns,
                    ])
                elif stat.S_ISDIR(entry_stat.st_mode) and not name.startswith("."):
                    subdirectories.append(name)
            subdirectories.reverse()
            stack.append((directory_fd, prefix, subdirectories))
            directory_fd = -1
            while stack and directory_fd < 0:
                parent_fd, parent_prefix, remaining = stack[-1]
                if not remaining:
                    stack.pop()
                    if parent_fd != session_fd:
                        os.close(parent_fd)
                    continue
                name = remaining.pop()
                try:
                    directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                except OSError:
                    continue
                prefix = parent_prefix + name + "/"
            if directory_fd < 0:
                break
    finally:
        for directory_fd, _, _ in stack:
            if directory_fd != session_fd:
                os.close(directory_fd)
        os.close(session_fd)
    return rows, None


def _remove_tree(args: dict[str, Any]) -> tuple[Any, int | None]:
    """Remove a session tree, bottom-up, without ever following a symlink.

    Every directory is opened with `O_NOFOLLOW` relative to its parent's descriptor, and every
    entry is removed with `unlink`/`rmdir` relative to its parent's descriptor; a symlink is
    unlinked, never traversed. Executed code owns the files it creates, including their
    permission bits, so it can leave behind a mode-000 directory: `CAP_DAC_OVERRIDE`, which the
    helper always keeps, gets past that, and `CAP_FOWNER` past a sticky bit. One descriptor per
    level is kept open.
    """
    session = _session_name(args)
    session_fd = _open_session(args)
    try:
        session_names = os.listdir(session_fd)
    except BaseException:
        os.close(session_fd)
        raise
    # Each level: its descriptor, its name in its parent, and the entries still to remove.
    stack: list[tuple[int, str, list[str]]] = [(session_fd, session, session_names)]
    try:
        while stack:
            directory_fd, directory_name, remaining = stack[-1]
            if not remaining:
                stack.pop()
                os.close(directory_fd)
                parent_fd = stack[-1][0] if stack else _root_fd
                os.rmdir(directory_name, dir_fd=parent_fd)
                continue
            name = remaining.pop()
            try:
                os.unlink(name, dir_fd=directory_fd)
                continue
            except FileNotFoundError:
                continue
            except IsADirectoryError:
                pass
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                # No longer a directory: executed code swapped it after the unlink attempt.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=directory_fd)
                continue
            try:
                child_names = os.listdir(child_fd)
            except BaseException:
                os.close(child_fd)
                raise
            stack.append((child_fd, name, child_names))
    finally:
        for directory_fd, _, _ in stack:
            os.close(directory_fd)
    return None, None


def _ping(args: dict[str, Any]) -> tuple[Any, int | None]:
    return None, None


_OPERATIONS: dict[str, Callable[[dict[str, Any]], tuple[Any, int | None]]] = {
    "ping": _ping,
    "create_session": _create_session,
    "hand_over": _hand_over,
    "open_read": _open_read,
    "stage": _stage,
    "commit": _commit,
    "discard": _discard,
    "delete": _delete,
    "list": _list,
    "snapshot": _snapshot,
    "remove_tree": _remove_tree,
}


def _send(message: dict[str, Any], fd: int | None) -> None:
    body = json.dumps(message).encode()
    data = len(body).to_bytes(4, "big") + body
    with _send_lock:
        if fd is None:
            _socket.sendall(data)
            return
        # The descriptor rides on the first sendmsg; a short write leaves the rest to sendall.
        sent = socket.send_fds(_socket, [data], [fd])
        if sent < len(data):
            _socket.sendall(data[sent:])


def _handle(request: Any) -> None:
    request_id = request.get("id") if isinstance(request, dict) else None
    fd = None
    try:
        operation = _OPERATIONS.get(request.get("op")) if isinstance(request, dict) else None
        args = request.get("args") if isinstance(request, dict) else None
        if operation is None or not isinstance(args, dict):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        result, fd = operation(args)
        response = {"id": request_id, "ok": result, "fd": fd is not None}
    except _Reject:
        response = {"id": request_id, "errno": errno.ENOENT}
    except OSError as exc:
        response = {"id": request_id, "errno": exc.errno or errno.EIO}
    except Exception:
        _log(f"unexpected error handling {request!r}:\n{traceback.format_exc()}")
        response = {"id": request_id, "errno": errno.EIO}
    try:
        _send(response, fd)
    except OSError as exc:
        # The API end is gone; the main loop sees the EOF and exits.
        _log(f"failed to send a response: {exc}")
    finally:
        if fd is not None:
            # The API received its own copy; this one would only pin the file open.
            os.close(fd)


def _receive_exactly(size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = _socket.recv(size - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return bytes(buffer)


def main() -> int:
    global _root_fd, _container_id, _socket
    if len(sys.argv) != 3:
        _log("usage: fs_helper.py <session root> <container uid>")
        return 2
    _container_id = int(sys.argv[2])
    try:
        _root_fd = os.open(sys.argv[1], _DIRECTORY_FLAGS)
    except OSError as exc:
        _log(f"cannot open the session root {sys.argv[1]!r}: {exc}")
        return 1
    _socket = socket.socket(fileno=0)

    while True:
        header = _receive_exactly(4)
        if header is None:
            return 0
        body = _receive_exactly(int.from_bytes(header, "big"))
        if body is None:
            return 0
        try:
            request = json.loads(body)
        except ValueError:
            _log("malformed request, exiting")
            return 1
        # One thread per request, so that a long `remove_tree` or `snapshot` does not hold up
        # other sessions' operations. Daemon threads: the API closing the socket ends the helper,
        # whatever is still running.
        threading.Thread(target=_handle, args=(request,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
