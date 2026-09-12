import argparse
import asyncio
import json
import re

import aiohttp
from yarl import URL

# Code runs attached to a PTY, so runtimes treat stdout as a terminal and may colourise it
# (Node renders booleans in yellow, for instance). That is intended - the sandbox ships
# lolcat and cmatrix - so assertions on output have to look past the escape sequences.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


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
                # part.filename resolves the RFC 5987 `filename*` parameter, so a nested
                # attachment arrives as its real sub_path ("out/file.txt") with no decoding
                # needed here.
                files[part.filename] = await part.read(decode=False)
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


async def check_javascript_module_styles(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    snippets = {
        "commonjs": "const os = require('node:os'); console.log('ok', typeof os.platform);",
        "esm": "import os from 'node:os'; console.log('ok', typeof os.platform);",
        "top-level await": "console.log('ok', await Promise.resolve('typeof'));",
    }
    for style, code in snippets.items():
        result = await _execute(session, api_url, "javascript", code, session_id=session_id)
        assert result["return_code"] == 0 and "ok" in _plain(result["output"]), f"javascript {style} failed: {result}"
        print(f"Execute javascript ({style}) -> {result['output']!r}")

    result = await _execute(
        session, api_url, "javascript",
        "const fs = require('node:fs'); console.log(fs.readFileSync('./sibling.txt', 'utf8').trim());",
        session_id=session_id, attachments={"sibling.txt": b"read from the session directory"},
    )
    assert result["return_code"] == 0 and "read from the session directory" in result["output"], (
        f"javascript could not read a session file through a relative path: {result}"
    )
    print(f"Execute javascript (relative path into the session dir) -> {result['output']!r}")


async def check_typescript(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    snippets = {
        "annotations": "import os from 'node:os';\nconst platform: string = os.platform();\nconsole.log('ok', platform.length > 0);",
        "commonjs": "const os = require('node:os');\ninterface Info { name: string }\nconst info: Info = { name: os.platform() };\nconsole.log('ok', info.name.length > 0);",
        # `enum` is not erasable syntax, so it only works because tsx transpiles rather
        # than relying on Node's native type stripping.
        "enum": "enum Level { Low, High }\nconsole.log('ok', Level[Level.High] === 'High');",
    }
    for style, code in snippets.items():
        result = await _execute(session, api_url, "typescript", code, session_id=session_id)
        assert result["return_code"] == 0 and "ok true" in _plain(result["output"]), f"typescript {style} failed: {result}"
        assert not result["files"], f"typescript {style} left files behind in the session: {sorted(result['files'])}"
        print(f"Execute typescript ({style}) -> {result['output']!r}")

    result = await _execute(
        session, api_url, "typescript",
        "import { greeting } from './greeter.ts';\nconsole.log(greeting);",
        session_id=session_id, attachments={"greeter.ts": b"export const greeting: string = 'imported from the session';"},
    )
    assert result["return_code"] == 0 and "imported from the session" in result["output"], (
        f"typescript could not import a session module through a relative specifier: {result}"
    )
    print(f"Execute typescript (relative import from the session dir) -> {result['output']!r}")


async def _new_session(session: aiohttp.ClientSession, api_url: str) -> str:
    async with session.post(f"{api_url}/sessions") as response:
        assert response.status == 200, f"POST /sessions failed: {response.status}"
        return (await response.json())["session_id"]


async def check_directory_listing(session: aiohttp.ClientSession, api_url: str, session_id: str) -> None:
    async with session.get(f"{api_url}/sessions/{session_id}/files/") as response:
        assert response.status == 200, f"Expected 200 listing the session root, got {response.status}"
        listing = await response.json()
    names = {entry["name"]: entry for entry in listing["entries"]}
    print(f"Root listing: {sorted(names)}")

    assert listing["path"] == "", f"Expected an empty path for the root listing, got {listing['path']!r}"
    assert "hello.txt" in names, f"Root listing is missing a known file: {sorted(names)}"
    assert names["hello.txt"]["type"] == "file", f"hello.txt typed as {names['hello.txt']['type']!r}"
    assert names["hello.txt"]["size"] > 0, "hello.txt reported as empty"
    assert names["leak"]["type"] == "symlink", (
        f"A symlink must be reported as such and never followed, got {names['leak']['type']!r}"
    )

    # Unlike execution results, a listing hides nothing -- it is how a caller discovers files
    # that /execute excluded or omitted.
    await _execute(session, api_url, "bash", "mkdir -p listdir/.hidden && echo x > listdir/.hidden/secret.txt && echo y > listdir/plain.txt", session_id=session_id)
    async with session.get(f"{api_url}/sessions/{session_id}/files/listdir") as response:
        assert response.status == 200, f"Expected 200 listing a subdirectory, got {response.status}"
        nested = await response.json()
    nested_names = {entry["name"]: entry for entry in nested["entries"]}
    assert nested["path"] == "listdir", f"Unexpected listing path: {nested['path']!r}"
    assert sorted(nested_names) == [".hidden", "plain.txt"], f"Unexpected listing: {sorted(nested_names)}"
    assert nested_names[".hidden"]["type"] == "directory", "A hidden directory must still be listed"
    print(f"Subdirectory listing: {sorted(nested_names)} (hidden entries included)")

    async with session.get(f"{api_url}/sessions/{session_id}/files/listdir/plain.txt") as response:
        assert response.status == 200, f"Expected 200 reading a file, got {response.status}"
        assert (await response.read()) == b"y\n", "A file GET must still return raw bytes, not a listing"
    print("GET on a file still returns bytes (as expected)")

    async with session.get(f"{api_url}/sessions/{session_id}/files/escapedir") as response:
        assert response.status == 404, (
            f"Expected 404 listing through a symlinked directory, got {response.status}"
        )
    print("GET listing on a symlinked directory -> 404 (as expected, not followed)")


async def check_result_attachment_cap(session: aiohttp.ClientSession, api_url: str, max_result_attachments: int) -> None:
    session_id = await _new_session(session, api_url)
    try:
        total = max_result_attachments + 50
        result = await _execute(
            session, api_url, "bash",
            f"for i in $(seq 1 {total}); do printf 'content %s' \"$i\" > \"out_$i.txt\"; done",
            session_id=session_id,
        )
        omitted = result["omitted_files"]
        print(f"Created {total} files -> {len(result['files'])} attachments, {len(omitted)} omitted")

        assert result["return_code"] == 0, f"Creating {total} files failed: {result['output']!r}"
        assert len(result["files"]) == max_result_attachments, (
            f"Expected exactly {max_result_attachments} attachments, got {len(result['files'])}"
        )
        assert len(omitted) == total - max_result_attachments, (
            f"Expected {total - max_result_attachments} omitted files, got {len(omitted)}"
        )
        assert not (set(result["files"]) & set(omitted)), "A file was both attached and omitted"
        assert len(set(result["files"]) | set(omitted)) == total, "Some changed files were reported nowhere"

        # An omitted file is not a lost file: it must still be retrievable individually.
        probe = omitted[0]
        async with session.get(f"{api_url}/sessions/{session_id}/files/{probe}") as response:
            assert response.status == 200, f"Expected 200 fetching omitted file {probe}, got {response.status}"
            assert (await response.read()).startswith(b"content "), f"Unexpected content for {probe}"
        print(f"Omitted file {probe} still retrievable via the files API (as expected)")

        # A session over the attachment cap must stay usable, not become permanently broken.
        result = await _execute(session, api_url, "bash", "echo still-alive", session_id=session_id)
        assert result["return_code"] == 0, f"Follow-up execute on a large session failed: {result}"
        assert not result["files"] and not result["omitted_files"], (
            f"A no-op run reported changes: {sorted(result['files'])} / {result['omitted_files']}"
        )
        print("Follow-up execute on the same over-cap session -> ok, nothing reported as changed")
    finally:
        await session.delete(f"{api_url}/sessions/{session_id}")


async def check_change_detection_stability(session: aiohttp.ClientSession, api_url: str) -> None:
    session_id = await _new_session(session, api_url)
    try:
        async with session.put(f"{api_url}/sessions/{session_id}/files/uploaded.txt", data=b"uploaded") as response:
            assert response.status == 204, f"PUT failed: {response.status}"

        result = await _execute(
            session, api_url, "bash",
            "mkdir -p made/deeper && echo created > made/deeper/by_container.txt",
            session_id=session_id,
        )
        assert "made/deeper/by_container.txt" in result["files"], (
            f"A container-created file was not returned: {sorted(result['files'])}"
        )
        assert "uploaded.txt" not in result["files"], "An untouched upload was reported as changed"

        # Both the API-created and the container-created file must now be treated as unchanged.
        # Before identity mapping the container-created one was re-sent on every single run.
        for attempt in (1, 2):
            result = await _execute(session, api_url, "bash", "true", session_id=session_id)
            assert not result["files"], (
                f"No-op run {attempt} re-reported unchanged files: {sorted(result['files'])}"
            )
            assert not result["deleted_files"], f"No-op run {attempt} reported deletions: {result['deleted_files']}"
        print("Repeated no-op runs report no changes for API- and container-created files (as expected)")

        # Rewriting with different content must still be caught, in both locations.
        result = await _execute(
            session, api_url, "bash",
            "echo rewritten > made/deeper/by_container.txt && echo rewritten > uploaded.txt",
            session_id=session_id,
        )
        assert set(result["files"]) == {"made/deeper/by_container.txt", "uploaded.txt"}, (
            f"Rewrites were not detected: {sorted(result['files'])}"
        )
        print("Rewrites detected for both file origins (as expected)")

        result = await _execute(session, api_url, "bash", "rm uploaded.txt", session_id=session_id)
        assert result["deleted_files"] == ["uploaded.txt"], f"Deletion not detected: {result['deleted_files']}"
        print("Deletion still detected (as expected)")
    finally:
        await session.delete(f"{api_url}/sessions/{session_id}")


async def check_home_directory_noise_excluded(session: aiohttp.ClientSession, api_url: str) -> None:
    session_id = await _new_session(session, api_url)
    try:
        # The session directory is the container's $HOME, so tool caches land in it. They must not
        # drown the real results, but must remain visible through the files API.
        result = await _execute(
            session, api_url, "bash",
            "mkdir -p .cache/pip .local/lib && echo junk > .cache/pip/blob && echo junk > .local/lib/mod.py && echo real > result.txt",
            session_id=session_id,
        )
        assert sorted(result["files"]) == ["result.txt"], (
            f"Hidden directories leaked into the attachments: {sorted(result['files'])}"
        )
        print(f"Hidden $HOME directories excluded from attachments (got {sorted(result['files'])})")

        async with session.get(f"{api_url}/sessions/{session_id}/files/.cache/pip") as response:
            assert response.status == 200, f"Excluded files must stay listable, got {response.status}"
            names = [entry["name"] for entry in (await response.json())["entries"]]
        assert names == ["blob"], f"Unexpected listing of an excluded directory: {names}"
        print("Excluded directories are still reachable through the files API (as expected)")
    finally:
        await session.delete(f"{api_url}/sessions/{session_id}")


async def check_inode_quota_enforcement(session: aiohttp.ClientSession, api_url: str, max_session_entries: int) -> None:
    session_id = await _new_session(session, api_url)
    try:
        probe = max_session_entries + 256
        result = await _execute(
            session, api_url, "bash",
            f"mkdir -p flood && cd flood && for i in $(seq 1 {probe}); do : > \"f_$i\" || exit 1; done",
            session_id=session_id,
        )
        print(f"Inode quota probe ({probe} empty files): return_code={result['return_code']} output={result['output']!r}")
        assert result["return_code"] != 0, (
            f"Creating {probe} files past --max-session-entries succeeded -- the session directory "
            "does not appear to be under an XFS project inode quota (check SESSION_QUOTA_MOUNTPOINT, "
            "that the filesystem is mounted prjquota rather than pqnoenforce, and that ihard was applied)"
        )
        assert "no space" in result["output"].lower(), (
            f"Expected an ENOSPC error (XFS reports project quotas as ENOSPC, not EDQUOT): {result['output']!r}"
        )
    finally:
        await session.delete(f"{api_url}/sessions/{session_id}")


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


async def run(
        api_url: str, *, check_quota: bool, max_session_size: int,
        max_session_entries: int, max_result_attachments: int,
) -> None:
    async with aiohttp.ClientSession() as session:
        await check_health(session, api_url)
        await check_seeded_session(session, api_url)

        session_id = await _new_session(session, api_url)
        print(f"Created session: {session_id}")

        await check_file_lifecycle(session, api_url, session_id)
        await check_execute_persistence(session, api_url, session_id)
        await check_execute_attachments(session, api_url, session_id)
        await check_symlink_attachment_excluded(session, api_url, session_id)
        await check_symlink_directory_escape_blocked(session, api_url, session_id)
        await check_readonly_root_filesystem(session, api_url, session_id)
        await check_directory_listing(session, api_url, session_id)
        if check_quota:
            await check_disk_quota_enforcement(session, api_url, session_id, max_session_size)
            await check_inode_quota_enforcement(session, api_url, max_session_entries)
        else:
            print("Skipping disk/inode quota checks (pass --check-quota once SESSION_QUOTA_MOUNTPOINT is configured)")
        await check_rejected_execute_leaves_no_attachment(session, api_url, session_id)
        await check_javascript_module_styles(session, api_url, session_id)
        await check_typescript(session, api_url, session_id)
        # These run on their own sessions: they create enough files to disturb the shared one.
        await check_change_detection_stability(session, api_url)
        await check_home_directory_noise_excluded(session, api_url)
        await check_result_attachment_cap(session, api_url, max_result_attachments)
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
        help="Also verify MAX_SESSION_SIZE and MAX_SESSION_ENTRIES are enforced from inside the container "
             "(requires the server's SESSION_QUOTA_MOUNTPOINT to be configured with a working XFS project quota setup)",
    )
    parser.add_argument(
        "--max-session-size", type=int, default=104_857_600,
        help="The server's configured MAX_SESSION_SIZE in bytes, used to size the --check-quota probe write (default: 100 MiB)",
    )
    parser.add_argument(
        "--max-session-entries", type=int, default=32768,
        help="The server's configured MAX_SESSION_ENTRIES, used to size the --check-quota inode probe (default: 32768)",
    )
    parser.add_argument(
        "--max-result-attachments", type=int, default=256,
        help="The server's configured MAX_RESULT_ATTACHMENTS, used by the partial-result check (default: 256)",
    )
    args = parser.parse_args()
    asyncio.run(run(
        args.api_url,
        check_quota=args.check_quota,
        max_session_size=args.max_session_size,
        max_session_entries=args.max_session_entries,
        max_result_attachments=args.max_result_attachments,
    ))
