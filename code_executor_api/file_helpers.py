from __future__ import annotations

import contextlib
import io
import os
import re
import secrets
from typing import Awaitable, Callable
from urllib.parse import quote

from aiofiles import open as aopen
from aiohttp import BodyPartReader, IOBasePayload, StreamReader
from aiohttp.hdrs import CONTENT_DISPOSITION
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

    def reduced_max(self, max_size: int) -> ContentSizeLimiter:
        if self.max_size > max_size:
            return ContentSizeLimiter(max_size)
        return self


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

    if isinstance(content, BodyPartReader):
        return content.read_chunk, content.at_eof
    if isinstance(content, StreamReader):
        return content.read, content.at_eof

    raise TypeError(f"Unsupported type for content")


async def _write_content(
        f,
        reader: _Reader,
        at_eof: _EOFPredicate,
        max_size: int,
        size_limiter: ContentSizeLimiter | None,
) -> int:
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


async def write_file_content(
        f,
        content: SupportedContentType,
        max_size: int,
        size_limiter: ContentSizeLimiter | None = None,
) -> int:
    reader, at_eof = _prepare_reader(content)
    return await _write_content(f, reader, at_eof, max_size, size_limiter)


async def read_content(content: SupportedContentType, max_size: int, size_limiter: ContentSizeLimiter | None = None) -> bytes:
    reader, at_eof = _prepare_reader(content)
    writer = io.BytesIO()
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
        writer.write(chunk)
    return writer.getvalue()


async def write_file_to_temp(
        parent_fd: int,
        content: SupportedContentType,
        max_size: int,
        size_limiter: ContentSizeLimiter | None = None,
) -> tuple[str, int]:
    """Write `content` to a hidden temp file inside the directory referenced by `parent_fd`,
    without making it visible under any final name yet. Returns (temporary_name, size); the
    caller decides whether to reveal it (`os.replace` it into place) or discard it."""
    temporary_name = f".code_executor_upload_{secrets.token_urlsafe(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
    try:
        async with aopen(fd, "wb", closefd=True) as f:
            fd = None
            size = await write_file_content(f, content, max_size, size_limiter)
    except BaseException:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        raise
    return temporary_name, size


_NON_ASCII_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _content_disposition(sub_path: str, field_name: str | None) -> str:
    """Build a `Content-Disposition` that can carry a full session sub_path.

    A plain `filename` cannot portably hold one: RFC 6266 tells recipients to strip
    directory components, and percent-escapes have no defined meaning there, so a client
    reading `filename="out%2Ff.txt"` gets that string literally. The exact sub_path
    therefore goes in the RFC 5987 `filename*` extended parameter - which clients decode
    back to `out/f.txt` - and `filename` keeps a flattened ASCII fallback for clients that
    only understand it.

    `filename*` percent-encodes everything outside the unreserved set, so a name chosen by
    executed code cannot smuggle a quote or a CRLF into the header.
    """
    fallback = _NON_ASCII_FILENAME.sub("_", sub_path.replace("/", "_")) or "attachment"
    parameters = [f'filename="{fallback}"', f"filename*=UTF-8''{quote(sub_path, safe='')}"]
    if field_name is not None:
        parameters.insert(0, f'name="{field_name}"')
    return "; ".join(["attachment", *parameters])


def read_file(fd: int, *, filename: str | None = None, field_name: str | None = None) -> IOBasePayload:
    f = os.fdopen(fd, "rb")  # Will be closed automatically by IOBasePayload
    try:
        # The filename is passed to the payload so it can guess a Content-Type from the
        # extension; the disposition header it derives from it is replaced below.
        payload = IOBasePayload(f, filename=filename) if filename is not None else IOBasePayload(f)
    except BaseException:
        f.close()
        raise
    if filename is not None:
        payload.headers[CONTENT_DISPOSITION] = _content_disposition(filename, field_name)
    elif field_name is not None:
        payload.set_content_disposition("attachment", name=field_name)
    return payload
