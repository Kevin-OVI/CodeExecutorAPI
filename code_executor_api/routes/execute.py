import contextlib
import logging

from aiohttp import MultipartWriter, web
from aiohttp.http_exceptions import BadHttpMessage

from ..config import MAX_CODE_LENGTH, MAX_RESULT_ATTACHMENTS
from ..executor import run_code_async
from ..file_helpers import read_content
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

    # Checked before anything else, including creating the ephemeral session: request.multipart()
    # fails inside aiohttp's own parser on a body that is not multipart, so there is no point
    # standing a session up only to tear it down again for a request already known to be bad.
    if request.content_type != "multipart/form-data":
        return web.json_response({"error": "Request must be multipart/form-data"}, status=400)

    try:
        if ephemeral:
            session_id = (await session_manager.create()).id
        assert session_id is not None

        async with session_manager.locked(session_id) as session:
            language: str | None = None
            code: str | None = None
            staged: list[tuple[str, str]] = []

            try:
                try:
                    reader = await request.multipart()
                    async for part in reader:
                        if part.name == "language":
                            try:
                                language = (await _read_text_part(part, 64)).strip()
                            except web.HTTPRequestEntityTooLarge:
                                # The cap itself is right - the field should not be unbounded -
                                # but a string that long names no supported language, which is
                                # invalid input, not a request-size problem.
                                raise ValidationError("Language is unsupported") from None
                        elif part.name == "code":
                            code = await _read_text_part(part, MAX_CODE_LENGTH)
                        elif part.name == "attachments":
                            if part.filename is None:
                                return web.json_response({"error": "attachments parts must be files"}, status=400)
                            staged.append(await session.stage_file(part.filename, part))
                        else:
                            return web.json_response({"error": f"Unsupported multipart field: {part.name}"}, status=400)
                except (AssertionError, ValueError, BadHttpMessage) as exc:
                    # A body that claims multipart but is malformed some other way fails inside
                    # the parser rather than in any validation of ours: a missing boundary
                    # parameter raises ValueError, and a part header aiohttp cannot parse - a NUL
                    # in an attachment filename, say - raises BadHttpMessage, which carries a 400
                    # code but is not an HTTPException, so nothing converts it on its own.
                    #
                    # Nothing else raised above derives from any of these: the API's own errors
                    # are plain Exceptions or HTTPExceptions, and the one ValueError subclass in
                    # reach, UnicodeDecodeError, is already converted by _read_text_part.
                    raise ValidationError("Request body is not valid multipart/form-data") from exc

                validate_language(language)
                validate_code(code)
                assert language is not None and code is not None

                # Drop each entry as it lands, so the `finally` below discards exactly those
                # attachments that were never committed. This is not atomic - os.replace has
                # overwritten the previous content by the time a later one fails - but the
                # discard list stays accurate either way: a committed entry is never queued for
                # a discard that would apply to whatever now holds its name.
                while staged:
                    normalised_sub_path, temporary_name = staged[0]
                    await session.commit_staged_file(normalised_sub_path, temporary_name)
                    staged.pop(0)
            finally:
                for normalised_sub_path, temporary_name in staged:
                    with contextlib.suppress(OSError):
                        await session.discard_staged_file(normalised_sub_path, temporary_name)

            async with session_manager.execution_slot():
                result = await run_code_async(session, language, code)

                # Split the run's changed files into what this response carries and what it
                # only names. Everything is opened up front because the JSON part comes first
                # and has to already know the final omitted list, so a file that turns out to
                # be unreadable has to be discovered before that part is written.
                #
                # `changed_files` is sorted, so the cut is deterministic and `omitted_files`
                # comes out sorted too. An unreadable file does not consume an attachment
                # slot, so a full response still carries MAX_RESULT_ATTACHMENTS files.
                payloads = []
                omitted_files = []
                for sub_path in result.changed_files:
                    if len(payloads) >= MAX_RESULT_ATTACHMENTS:
                        omitted_files.append(sub_path)
                        continue
                    try:
                        payloads.append(session.read_file(sub_path, field_name="attachments"))
                    except (OSError, UnicodeError):
                        LOGGER.warning("Omitting unreadable result attachment %s", sub_path)
                        omitted_files.append(sub_path)

                with MultipartWriter("mixed") as mpwriter:
                    result_payload = mpwriter.append_json(
                        {
                            "output": result.execution_result.output,
                            "return_code": result.execution_result.return_code,
                            "execution_time": result.execution_result.execution_time,
                            "timed_out": result.execution_result.timed_out,
                            "deleted_files": result.deleted_files,
                            "omitted_files": omitted_files,
                        },
                    )
                    result_payload.set_content_disposition("inline", name="result")
                    for payload in payloads:
                        mpwriter.append_payload(payload)
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
