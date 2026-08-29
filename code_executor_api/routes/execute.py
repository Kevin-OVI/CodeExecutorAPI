import contextlib
import logging
import os

from aiofiles.tempfile import NamedTemporaryFile as AsyncNamedTemporaryFile
from aiohttp import MultipartWriter, web

from ..config import CONTAINER_ULIMIT_FSIZE, MAX_REQUEST_SIZE, MAX_SESSION_FILES, SESSION_ROOT_DIRECTORY
from ..executor import ExecutionResourceLimitReached, run_code_async
from ..file_helpers import ContentSizeLimiter, read_content, write_file_content
from ..sessions import (
    ExecutionLimitReached,
    SessionLimitReached,
    SessionLockTimeout,
    SessionManager,
    SessionNotFound,
    SessionResourceLimitReached,
)
from ..validation import ValidationError, normalize_sub_path, validate_code, validate_language

__all__ = ("handle_execute",)
LOGGER = logging.getLogger(__name__)


@contextlib.contextmanager
def cleanup_attachments():
    attachments: list[tuple[str, str]] = []
    try:
        yield attachments
    finally:
        for _, src_path in attachments:
            with contextlib.suppress(OSError):
                os.remove(src_path)


async def _read_text_part(part, max_size: int, size_limiter: ContentSizeLimiter) -> str:
    content = await read_content(part, max_size, size_limiter)
    try:
        return content.decode(part.get_charset(default="utf-8"))
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValidationError("Multipart text fields must use a valid character encoding") from exc


async def handle_execute(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]

    session_id: str | None = None
    language: str | None = None
    code: str | None = None
    size_limiter = ContentSizeLimiter(MAX_REQUEST_SIZE)
    part_count = 0

    with cleanup_attachments() as attachments:
        reader = await request.multipart()
        async for part in reader:
            part_count += 1
            if part_count > MAX_SESSION_FILES:
                return web.json_response({"error": "Too many multipart fields"}, status=413)
            if part.name == "session_id":
                text = (await _read_text_part(part, 256, size_limiter)).strip()
                session_id = text or None
            elif part.name == "language":
                language = (await _read_text_part(part, 64, size_limiter)).strip()
            elif part.name == "code":
                code = await _read_text_part(part, MAX_REQUEST_SIZE, size_limiter)
            elif part.name == "attachments":
                if part.filename is None:
                    return web.json_response({"error": "attachments parts must be files"}, status=400)
                normalised_path = normalize_sub_path(part.filename)
                async with AsyncNamedTemporaryFile("wb", delete=False, dir=SESSION_ROOT_DIRECTORY) as f:
                    attachments.append((normalised_path, f.name))
                    await write_file_content(f, part, CONTAINER_ULIMIT_FSIZE, size_limiter)
            else:
                return web.json_response({"error": "Unsupported multipart field"}, status=400)

        validate_language(language)
        validate_code(code)

        ephemeral = session_id is None
        try:
            if ephemeral:
                session_id = session_manager.create().id
            assert session_id is not None and language is not None and code is not None

            async with session_manager.locked(session_id) as session:
                async with session_manager.execution_slot():
                    for sub_path, src_path in attachments:
                        session.import_file(sub_path, src_path)

                    result = await run_code_async(session.work_directory, language, code)

                    with MultipartWriter("mixed") as mpwriter:
                        result_payload = mpwriter.append_json(
                            {
                                "output": result.execution_result.output,
                                "return_code": result.execution_result.return_code,
                                "execution_time": result.execution_result.execution_time,
                                "timed_out": result.execution_result.timed_out,
                                "deleted_files": result.deleted_files,
                            },
                        )
                        result_payload.set_content_disposition("inline", name="result")
                        for attachment in result.attachments:
                            mpwriter.append_payload(session.read_file(attachment.sub_path, field_name="attachments"))
        except SessionNotFound:
            return web.json_response({"error": "Session not found"}, status=404)
        except SessionLockTimeout:
            return web.json_response({"error": "Session is busy"}, status=409)
        except SessionLimitReached:
            return web.json_response({"error": "Session capacity reached"}, status=503)
        except ExecutionLimitReached:
            return web.json_response({"error": "Execution capacity reached"}, status=503)
        except SessionResourceLimitReached as exc:
            return web.json_response({"error": str(exc)}, status=413)
        except ExecutionResourceLimitReached as exc:
            return web.json_response({"error": str(exc)}, status=413)
        except (FileNotFoundError, IsADirectoryError):
            return web.json_response({"error": "Invalid attachment path"}, status=400)
        finally:
            if ephemeral and session_id is not None:
                with contextlib.suppress(SessionNotFound):
                    await session_manager.delete(session_id)

    return web.Response(body=mpwriter, headers=mpwriter.headers)
