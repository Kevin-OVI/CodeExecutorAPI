import contextlib
import os
import secrets
from typing import Any, Awaitable, Callable

from aiofiles import open as aopen
from aiohttp import BodyPartReader, IOBasePayload, StreamReader
from aiohttp.web import HTTPRequestEntityTooLarge

type _Reader = Callable[[int], Awaitable[bytes]]
type _EOFPredicate = Callable[[], bool]
type SupportedContentType = BodyPartReader | StreamReader | bytes


class ContentSizeLimiter:
    def __init__(self, max_size: int):
        self.max_size = max_size
        self.actual_size = 0

    def add(self, size: int) -> None:
        self.actual_size += size
        if self.actual_size > self.max_size:
            raise HTTPRequestEntityTooLarge(max_size=self.max_size, actual_size=self.actual_size)


def _prepare_reader(content: SupportedContentType) -> tuple[_Reader, _EOFPredicate]:
    if isinstance(content, bytes):
        read_file = False

        async def reader(_) -> bytes:
            nonlocal read_file
            if read_file:
                return b""
            assert isinstance(content, bytes)
            read_file = True
            return content

        def at_eof():
            return read_file

        return reader, at_eof

    at_eof = content.at_eof
    if isinstance(content, BodyPartReader):
        reader = content.read_chunk
    elif isinstance(content, StreamReader):
        reader = content.read
    else:
        raise TypeError(f"Unsupported type for content")

    return reader, at_eof


async def _write_content(f, reader: _Reader, at_eof: _EOFPredicate, max_size: int, size_limiter: ContentSizeLimiter | None) -> int:
    actual_size = 0
    while not at_eof():
        chunk = await reader(8192)
        if not chunk:
            break
        actual_size += len(chunk)
        if actual_size > max_size:
            raise HTTPRequestEntityTooLarge(max_size=max_size, actual_size=actual_size)
        if size_limiter is not None:
            size_limiter.add(len(chunk))
        await f.write(chunk)
    return actual_size


async def write_file_content(f, content: SupportedContentType, max_size: int, size_limiter: ContentSizeLimiter | None = None) -> int:
    reader, at_eof = _prepare_reader(content)
    return await _write_content(f, reader, at_eof, max_size, size_limiter)


async def read_content(content: SupportedContentType, max_size: int, size_limiter: ContentSizeLimiter | None = None) -> bytes:
    reader, at_eof = _prepare_reader(content)
    chunks = []
    actual_size = 0
    while not at_eof():
        chunk = await reader(8192)
        if not chunk:
            break
        actual_size += len(chunk)
        if actual_size > max_size:
            raise HTTPRequestEntityTooLarge(max_size=max_size, actual_size=actual_size)
        if size_limiter is not None:
            size_limiter.add(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


async def write_file_at_content(
        parent_fd: int,
        filename: str,
        content: SupportedContentType,
        max_size: int,
        validate_size: Callable[[int], None],
        size_limiter: ContentSizeLimiter | None = None,
) -> None:
    temporary_name = f".code_executor_upload_{secrets.token_urlsafe(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
    try:
        async with aopen(fd, "wb", closefd=True) as f:
            fd = -1
            actual_size = await write_file_content(f, content, max_size, size_limiter)
        validate_size(actual_size)
        os.replace(temporary_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        if fd != -1:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        raise


def read_file(fd: int, *, filename: str | None = None, field_name: str | None = None) -> IOBasePayload:
    kwargs: dict[str, Any] = {}
    if filename is not None:
        kwargs["filename"] = filename
    f = os.fdopen(fd, "rb")  # Will be closed automatically by IOBasePayload
    try:
        payload = IOBasePayload(f, **kwargs)
    except BaseException:
        f.close()
        raise
    if field_name is not None:
        payload.set_content_disposition("attachment", **kwargs, name=field_name)
    return payload
