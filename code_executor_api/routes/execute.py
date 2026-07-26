import contextlib
import logging
import os

from aiofiles.tempfile import NamedTemporaryFile as AsyncNamedTemporaryFile
from aiohttp import MultipartWriter, web

from ..executor import run_code_async
from ..file_helpers import read_file, write_file_content
from ..sessions import SessionLockTimeout, SessionManager, SessionNotFound
from ..validation import validate_code, validate_language

__all__ = ("handle_execute",)
LOGGER = logging.getLogger(__name__)


@contextlib.contextmanager
def cleanup_attachments():
    attachments: list[tuple[str, str]] = []
    yield attachments

    for _, src_path in attachments:
        with contextlib.suppress(OSError):
            os.remove(src_path)


async def handle_execute(request: web.Request) -> web.Response:
    session_manager: SessionManager = request.app["session_manager"]

    session_id: str | None = None
    language: str | None = None
    code: str | None = None

    with cleanup_attachments() as attachments:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "session_id":
                text = (await part.text()).strip()
                session_id = text or None
            elif part.name == "language":
                language = (await part.text()).strip()
            elif part.name == "code":
                code = await part.text()
            elif part.name == "attachments":
                if part.filename is None:
                    return web.json_response({"error": "attachments parts must be files"}, status=400)
                async with AsyncNamedTemporaryFile("wb", delete=False) as f:
                    await write_file_content(f, part)
                attachments.append((part.filename, f.name))

        validate_language(language)
        validate_code(code)

        ephemeral = session_id is None
        if ephemeral:
            session_id = session_manager.create().id
        assert session_id is not None and language is not None and code is not None

        try:
            async with session_manager.locked(session_id) as session:
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
                        mpwriter.append_payload(read_file(attachment.absolute_path, filename=attachment.sub_path, field_name="attachments"))
        except SessionNotFound:
            return web.json_response({"error": "Session not found"}, status=404)
        except SessionLockTimeout:
            return web.json_response({"error": "Session is busy"}, status=409)
        finally:
            if ephemeral:
                with contextlib.suppress(SessionNotFound):
                    await session_manager.delete(session_id)

    return web.Response(body=mpwriter, headers=mpwriter.headers)
