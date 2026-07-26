from typing import Any, Awaitable, Callable

from aiofiles import open as aopen
from aiohttp import BodyPartReader, IOBasePayload, StreamReader

type _Reader = Callable[[int], Awaitable[bytes]]
type _EOFPredicate = Callable[[], bool]
type SupportedContentType = BodyPartReader | StreamReader | bytes


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


async def _write_content(f, reader: _Reader, at_eof: _EOFPredicate):
    while not at_eof():
        chunk = await reader(8192)
        await f.write(chunk)


async def write_file_content(f, content: SupportedContentType):
    reader, at_eof = _prepare_reader(content)
    await _write_content(f, reader, at_eof)


async def write_file_at_content(path: str, content: SupportedContentType):
    reader, at_eof = _prepare_reader(content)
    async with aopen(path, "wb") as f:
        await _write_content(f, reader, at_eof)


def read_file(path: str, *, filename: str | None = None, field_name: str | None = None) -> IOBasePayload:
    kwargs: dict[str, Any] = {}
    if filename is not None:
        kwargs["filename"] = filename
    f = open(path, "rb")  # Will be closed automatically by IOBasePayload
    payload = IOBasePayload(f, **kwargs)
    if field_name is not None:
        payload.set_content_disposition("attachment", **kwargs, name=field_name)
    return payload
