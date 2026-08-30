import contextlib
import logging

from aiohttp import MultipartWriter, web

from ..config import MAX_CODE_LENGTH, MAX_SESSION_SIZE
from ..executor import run_code_async
from ..file_helpers import ContentSizeLimiter, read_content
from ..sessions import (
    ExecutionLimitReached,
    QuotaSetupFailed,
    SessionLimitReached,
    SessionLockTimeout,
    SessionManager,
    SessionNotFound,
    SessionResourceLimitReached,
)
from ..validation import ValidationError, validate_code, validate_language

__all__ = ("handle_execute",)
LOGGER = logging.getLogger(__name__)


async def _read_text_part(part, max_size: int) -> str:
    content = await read_content(part, max_size)
    try:
        return content.decode(part.get_charset(default="utf-8"))
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValidationError("Multipart text fields must use a valid character encoding") from exc


async def handle_execute(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]
    session_id: str | None = request.match_info.get("session_id")
    ephemeral = session_id is None

    try:
        if ephemeral:
            session_id = (await session_manager.create()).id
        assert session_id is not None

        async with session_manager.locked(session_id) as session:
            language: str | None = None
            code: str | None = None
            size_limiter = ContentSizeLimiter(MAX_SESSION_SIZE)
            staged: list[tuple[str, str]] = []

            try:
                reader = await request.multipart()
                async for part in reader:
                    if part.name == "language":
                        language = (await _read_text_part(part, 64)).strip()
                    elif part.name == "code":
                        code = await _read_text_part(part, MAX_CODE_LENGTH)
                    elif part.name == "attachments":
                        if part.filename is None:
                            return web.json_response({"error": "attachments parts must be files"}, status=400)
                        staged.append(await session.stage_file(part.filename, part, size_limiter))
                    else:
                        return web.json_response({"error": f"Unsupported multipart field: {part.name}"}, status=400)

                validate_language(language)
                validate_code(code)
                assert language is not None and code is not None

                for normalised_sub_path, temporary_name in staged:
                    await session.commit_staged_file(normalised_sub_path, temporary_name)
                staged.clear()
            finally:
                for normalised_sub_path, temporary_name in staged:
                    with contextlib.suppress(OSError):
                        await session.discard_staged_file(normalised_sub_path, temporary_name)

            async with session_manager.execution_slot():
                result = await run_code_async(session, language, code)

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
    except QuotaSetupFailed:
        LOGGER.exception("Failed to apply session storage quota")
        return web.json_response({"error": "Session storage quota setup failed"}, status=500)
    except ExecutionLimitReached:
        return web.json_response({"error": "Execution capacity reached"}, status=503)
    except SessionResourceLimitReached as exc:
        return web.json_response({"error": str(exc)}, status=413)
    except (FileNotFoundError, IsADirectoryError):
        return web.json_response({"error": "Invalid attachment path"}, status=400)
    finally:
        if ephemeral and session_id is not None:
            with contextlib.suppress(SessionNotFound):
                await session_manager.delete(session_id)

    return web.Response(body=mpwriter, headers=mpwriter.headers)
