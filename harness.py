import argparse
import asyncio
import json

import aiohttp
from yarl import URL


async def _execute(
        session: aiohttp.ClientSession,
        api_url: str,
        language: str,
        code: str,
        *,
        session_id: str | None = None,
        attachments: dict[str, bytes] | None = None,
        expected_status: int = 200,
) -> dict | None:
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("language", language)
    form.add_field("code", code)
    for filename, content in (attachments or {}).items():
        form.add_field("attachments", content, filename=filename, content_type="application/octet-stream")

    url = f"{api_url}/sessions/{session_id}/execute" if session_id is not None else f"{api_url}/execute"
    async with session.post(url, data=form) as response:
        if response.status != expected_status:
            body = await response.text()
            raise RuntimeError(f"{url} expected status {expected_status}, got {response.status}: {body}")
        if response.status != 200:
            return None

        reader = aiohttp.MultipartReader.from_response(response)
        result = None
        files = {}
        async for part in reader:
            if part.headers.get(aiohttp.hdrs.CONTENT_TYPE) == "application/json":
                result = json.loads(await part.read(decode=False))
            else:
                filename = part.filename
                files[filename] = await part.read(decode=False)
        result["files"] = files
        return result


async def check_health(session: aiohttp.ClientSession, api_url: str) -> None:
    async with session.get(f"{api_url}/health") as response:
        assert response.status == 200, f"/health failed: {response.status}"
        data = await response.json()
        assert data.get("status") == "ok", f"Unexpected /health payload: {data!r}"
    print("GET /health -> 200 ok")


async def check_seeded_session(session: aiohttp.ClientSession, api_url: str) -> None:
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("seed.txt", b"seeded content", filename="seed.txt", content_type="application/octet-stream")
    async with session.post(f"{api_url}/sessions", data=form) as response:
        assert response.status == 200, f"Seeded session create failed: {response.status}"
        data = await response.json()
        session_id = data["session_id"]
    print(f"Created seeded session: {session_id}")

    async with session.get(f"{api_url}/sessions/{session_id}/files/seed.txt") as response:
        assert response.status == 200, f"GET seed.txt failed: {response.status}"
        content = await response.read()
        assert content == b"seeded content", f"Unexpected seeded content: {content!r}"
    print(f"GET seed.txt -> {content!r}")

    async with session.delete(f"{api_url}/sessions/{session_id}") as response:
        assert response.status == 204, f"DELETE seeded session failed: {response.status}"


async def check_file_lifecycle(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
        assert response.status == 204, f"PUT failed: {response.status}"
    print("PUT hello.txt -> 204")

    async with session.get(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
        assert response.status == 200, f"GET failed: {response.status}"
        content = await response.read()
        assert content == b"hello world", f"Unexpected content: {content!r}"
    print(f"GET hello.txt -> {content!r}")

    async with session.delete(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
        assert response.status == 204, f"DELETE file failed: {response.status}"
    print("DELETE hello.txt -> 204")

    async with session.get(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
        assert response.status == 404, f"Expected 404 after file deletion, got {response.status}"
    print("GET hello.txt after delete -> 404 (as expected)")

    # Restore hello.txt for the execute checks that follow.
    async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
        assert response.status == 204, f"PUT (restore) failed: {response.status}"


async def check_execute_persistence(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    result = await _execute(
        session, api_url, "python",
        "with open('hello.txt') as f:\n    data = f.read()\nwith open('output.txt', 'w') as f:\n    f.write(data.upper())\nprint('done')",
        session_id=session_id,
    )
    print(f"Execute #1: output={result['output']!r} return_code={result['return_code']} "
          f"files={list(result['files'])} deleted_files={result['deleted_files']}")
    assert result["files"].get("output.txt") == b"HELLO WORLD", "output.txt was not persisted correctly"

    result = await _execute(session, api_url, "python", "import os\nos.remove('output.txt')\nprint('removed')", session_id=session_id)
    print(f"Execute #2: output={result['output']!r} deleted_files={result['deleted_files']}")
    assert "output.txt" in result["deleted_files"], "output.txt deletion was not detected"


async def check_execute_attachments(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    result = await _execute(
        session, api_url, "bash", "cat attached.txt | tr a-z A-Z > attached_upper.txt",
        session_id=session_id, attachments={"attached.txt": b"attach me"},
    )
    print(f"Execute with attachment: output={result['output']!r} files={list(result['files'])}")
    assert result["files"].get("attached_upper.txt") == b"ATTACH ME", "attachment was not processed correctly"

    async with session.get(f"{api_url}/sessions/{session_id}/files/attached.txt") as response:
        assert response.status == 200, f"GET attached.txt failed: {response.status}"
        content = await response.read()
        assert content == b"attach me", f"Unexpected attached.txt content: {content!r}"
    print("GET attached.txt -> matches uploaded attachment")


async def check_symlink_attachment_excluded(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    result = await _execute(
        session, api_url, "python", "import os\nos.symlink('/etc/passwd', 'leak')\nprint('linked')",
        session_id=session_id,
    )
    print(f"Execute symlink creation: output={result['output']!r} files={list(result['files'])}")
    assert "leak" not in result["files"], "a symlink was exposed as an execute attachment"

    async with session.get(f"{api_url}/sessions/{session_id}/files/leak") as response:
        assert response.status == 404, f"Expected 404 reading a symlink, got {response.status}"
    print("GET symlinked file -> 404 (symlink not followed, as expected)")


async def check_symlink_directory_escape_blocked(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    result = await _execute(
        session, api_url, "python", "import os\nos.symlink('/tmp', 'escapedir')\nprint('linked dir')",
        session_id=session_id,
    )
    print(f"Execute symlinked-directory creation: output={result['output']!r} files={list(result['files'])}")

    async with session.get(f"{api_url}/sessions/{session_id}/files/escapedir/whatever.txt") as response:
        assert response.status == 404, f"Expected 404 reading through a symlinked directory, got {response.status}"
    print("GET through symlinked directory -> 404 (as expected)")

    async with session.put(f"{api_url}/sessions/{session_id}/files/escapedir/pwned.txt", data=b"pwned") as response:
        assert response.status == 400, f"Expected 400 writing through a symlinked directory, got {response.status}"
    print("PUT through symlinked directory -> 400 (as expected)")


async def check_readonly_root_filesystem(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    result = await _execute(session, api_url, "bash", "touch /pwned.txt", session_id=session_id)
    print(f"Write outside /app: output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] != 0, "Writing outside the mounted /app directory unexpectedly succeeded"
    assert "read-only" in result["output"].lower(), f"Unexpected failure mode: {result['output']!r}"


async def check_disk_quota_enforcement(session: aiohttp.ClientSession, api_url: str, session_id: str, max_session_size: int) -> None:
    result = await _execute(session, api_url, "bash", "dd if=/dev/zero of=small.bin bs=1M count=1 2>&1", session_id=session_id)
    print(f"Disk quota probe (1MiB write): output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] == 0, f"A small write well under the quota unexpectedly failed: {result['output']!r}"

    probe_mib = max_session_size // (1024 * 1024) + 16
    result = await _execute(session, api_url, "bash", f"dd if=/dev/zero of=quota_probe.bin bs=1M count={probe_mib} 2>&1", session_id=session_id)
    print(f"Disk quota probe ({probe_mib}MiB write): output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] != 0, (
        "Writing well past --max-session-size from inside the container succeeded -- "
        "the session directory does not appear to be under an XFS project quota "
        "(check SESSION_QUOTA_MOUNTPOINT and that xfs_quota is usable by the API process)"
    )
    assert "no space" in result["output"].lower(), f"Unexpected failure mode, expected an ENOSPC error: {result['output']!r}"


async def check_rejected_execute_leaves_no_attachment(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    await _execute(
        session, api_url, "not-a-real-language", "irrelevant",
        session_id=session_id, attachments={"should_not_persist.txt": b"should not persist"},
        expected_status=400,
    )
    print("Execute with bad language + attachment -> 400 (as expected)")

    async with session.get(f"{api_url}/sessions/{session_id}/files/should_not_persist.txt") as response:
        assert response.status == 404, (
            f"Expected the attachment from a rejected execute request to be absent, got {response.status}"
        )
    print("GET attachment from rejected execute -> 404 (as expected, nothing was left behind)")


async def check_ephemeral_execute(session: aiohttp.ClientSession, api_url: str) -> None:
    result = await _execute(session, api_url, "javascript", "console.log('from ephemeral session')")
    print(f"Ephemeral /execute: output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] == 0, f"Ephemeral execute failed: {result}"


async def check_error_cases(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    async with session.get(f"{api_url}/sessions/does-not-exist/files/hello.txt") as response:
        assert response.status == 404, f"Expected 404 for unknown session, got {response.status}"
    print("GET file on unknown session -> 404 (as expected)")

    async with session.delete(f"{api_url}/sessions/does-not-exist") as response:
        assert response.status == 404, f"Expected 404 deleting unknown session, got {response.status}"
    print("DELETE unknown session -> 404 (as expected)")

    await _execute(session, api_url, "cobol", "print", session_id=session_id, expected_status=400)
    print("Execute with unsupported language -> 400 (as expected)")

    traversal_url = URL(f"{api_url}/sessions/{session_id}/files/%2e%2e/outside.txt", encoded=True)
    async with session.get(traversal_url) as response:
        assert response.status == 400, f"Expected 400 for path traversal, got {response.status}"
    print("GET path escaping session root -> 400 (as expected)")


async def run(api_url: str, *, check_quota: bool, max_session_size: int) -> None:
    async with aiohttp.ClientSession() as session:
        await check_health(session, api_url)
        await check_seeded_session(session, api_url)

        async with session.post(f"{api_url}/sessions") as response:
            data = await response.json()
            session_id = data["session_id"]
        print(f"Created session: {session_id}")

        await check_file_lifecycle(session, api_url, session_id)
        await check_execute_persistence(session, api_url, session_id)
        await check_execute_attachments(session, api_url, session_id)
        await check_symlink_attachment_excluded(session, api_url, session_id)
        await check_symlink_directory_escape_blocked(session, api_url, session_id)
        await check_readonly_root_filesystem(session, api_url, session_id)
        if check_quota:
            await check_disk_quota_enforcement(session, api_url, session_id, max_session_size)
        else:
            print("Skipping disk quota check (pass --check-quota once SESSION_QUOTA_MOUNTPOINT is configured)")
        await check_rejected_execute_leaves_no_attachment(session, api_url, session_id)
        await check_ephemeral_execute(session, api_url)
        await check_error_cases(session, api_url, session_id)

        async with session.delete(f"{api_url}/sessions/{session_id}") as response:
            assert response.status == 204, f"DELETE session failed: {response.status}"
        print("DELETE session -> 204")

        async with session.get(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
            assert response.status == 404, f"Expected 404 after session deletion, got {response.status}"
        print("GET after delete -> 404 (as expected)")

    print("All harness checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local smoke test for the CodeExecutorAPI server")
    parser.add_argument("--api-url", default="http://127.0.0.1:40003", help="Base URL for the API server")
    parser.add_argument(
        "--check-quota", action="store_true",
        help="Also verify MAX_SESSION_SIZE is enforced from inside the container (requires the server's "
             "SESSION_QUOTA_MOUNTPOINT to be configured with a working XFS project quota setup)",
    )
    parser.add_argument(
        "--max-session-size", type=int, default=104_857_600,
        help="The server's configured MAX_SESSION_SIZE in bytes, used to size the --check-quota probe write (default: 100 MiB)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.api_url, check_quota=args.check_quota, max_session_size=args.max_session_size))
