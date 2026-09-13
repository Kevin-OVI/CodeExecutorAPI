import argparse
import asyncio
import contextlib
import dataclasses
import json
import pathlib
import re
import shutil
import subprocess
import time
import traceback
from typing import AbstractSet, AsyncIterator, Awaitable, Callable, Sequence
from urllib.parse import quote

import aiohttp
from yarl import URL

# Code runs attached to a PTY, so runtimes treat stdout as a terminal and may colourise it
# (Node renders booleans in yellow, for instance). That is intended - the sandbox ships
# lolcat and cmatrix - so assertions on output have to look past the escape sequences.
#
# Colour (SGR) is not the only thing that arrives: `dotnet` emits keypad-mode sequences
# (\x1b[?1h\x1b=) around its output, so this matches any CSI sequence and the two-character
# escapes, not just \x1b[...m.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[=>])")


def _plain(text: str) -> str:
    """Strip terminal escapes and fold CRLF.

    The PTY turns every newline into CRLF and only the trailing one is stripped server side,
    so multi-line output compared as a whole would never match without this.
    """
    return _ANSI_ESCAPE.sub("", text).replace("\r\n", "\n")


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    """The server's configured limits, mirrored so checks can size their probes.

    Every field mirrors one environment variable from code_executor_api/config.py; the CLI
    flag is the field name with underscores as dashes. Defaults match the server's own
    defaults, so a default-configured server needs no flags at all. Nothing validates these
    against the running server, so every assertion derived from one names the flag in its
    failure message.
    """
    max_session_size: int = dataclasses.field(default=104_857_600, metadata={
        "env": "MAX_SESSION_SIZE", "help": "bytes; sizes the quota probe write"})
    max_session_entries: int = dataclasses.field(default=32_768, metadata={
        "env": "MAX_SESSION_ENTRIES", "help": "inodes; sizes the quota inode probe"})
    max_result_attachments: int = dataclasses.field(default=256, metadata={
        "env": "MAX_RESULT_ATTACHMENTS", "help": "used by the partial-result check"})
    max_code_length: int = dataclasses.field(default=65_536, metadata={
        "env": "MAX_CODE_LENGTH", "help": "bytes; sizes the oversized-code probe"})
    max_output_size: int = dataclasses.field(default=10_485_760, metadata={
        "env": "MAX_OUTPUT_SIZE", "help": "bytes; sizes the output-truncation probe"})
    container_pids_limit: int = dataclasses.field(default=128, metadata={
        "env": "CONTAINER_PIDS_LIMIT", "help": "sizes the process-creation probe"})
    container_ulimit_nofile: int = dataclasses.field(default=1024, metadata={
        "env": "CONTAINER_ULIMIT_NOFILE", "help": "the RLIMIT_NOFILE the container must report"})
    max_file_size: int = dataclasses.field(default=268_435_456, metadata={
        "env": "CONTAINER_ULIMIT_FSIZE", "help": "bytes; sizes the per-file 413 probe"})
    execution_timeout: int = dataclasses.field(default=20, metadata={
        "env": "EXECUTION_TIMEOUT", "help": "seconds; sizes the overrun probe"})
    max_sessions: int = dataclasses.field(default=64, metadata={
        "env": "MAX_SESSIONS", "help": "sizes the capacity probe"})
    max_concurrent_executions: int = dataclasses.field(default=4, metadata={
        "env": "MAX_CONCURRENT_EXECUTIONS", "help": "sizes the concurrency probe"})
    lock_wait_timeout: int = dataclasses.field(default=30, metadata={
        "env": "SESSION_LOCK_WAIT_TIMEOUT_SECONDS", "help": "seconds; how long a 409 should take"})
    inactivity_timeout: int = dataclasses.field(default=1800, metadata={
        "env": "SESSION_INACTIVITY_TIMEOUT_SECONDS", "help": "seconds; how long expiry takes"})
    sweep_interval: int = dataclasses.field(default=60, metadata={
        "env": "SESSION_SWEEP_INTERVAL_SECONDS", "help": "seconds; added to the expiry wait"})
    session_root: str = dataclasses.field(default="", metadata={
        "env": "SESSION_ROOT_DIRECTORY",
        "help": "the server's session directory on this host, for the host-side checks"})


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """Generate one CLI flag per Config field, so adding a limit is a one-line edit."""
    for config_field in dataclasses.fields(Config):
        parser.add_argument(
            f"--{config_field.name.replace('_', '-')}",
            type=int if config_field.type is int else str, default=config_field.default,
            help=f"The server's configured {config_field.metadata['env']} - "
                 f"{config_field.metadata['help']} (default: {config_field.default})",
        )


def _config_from_args(args: argparse.Namespace) -> Config:
    return Config(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Config)})


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


async def _new_session(session: aiohttp.ClientSession, api_url: str) -> str:
    async with session.post(f"{api_url}/sessions") as response:
        assert response.status == 200, f"POST /sessions failed: {response.status}"
        return (await response.json())["session_id"]


@contextlib.asynccontextmanager
async def owned_sessions(
        session: aiohttp.ClientSession, api_url: str, count: int = 1,
) -> AsyncIterator[list[str]]:
    """Create `count` sessions and delete them all, whatever happens in between.

    The runner wraps every check in this, so a check body never creates or destroys a
    session of its own and a failed assertion still frees what it was given. A cleanup
    DELETE that fails only warns: turning it into a failure would blame this check for a
    broken DELETE, which `session_deletion` covers directly.
    """
    session_ids: list[str] = []
    try:
        for _ in range(count):
            session_ids.append(await _new_session(session, api_url))
        yield session_ids
    finally:
        for session_id in reversed(session_ids):
            try:
                async with session.delete(f"{api_url}/sessions/{session_id}") as response:
                    if response.status not in (204, 404):
                        print(f"WARNING: cleanup DELETE {session_id} -> {response.status}")
            except aiohttp.ClientError as exc:
                print(f"WARNING: cleanup DELETE {session_id} raised {exc!r}")


async def check_health(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    async with session.get(f"{api_url}/health") as response:
        assert response.status == 200, f"/health failed: {response.status}"
        data = await response.json()
        assert data.get("status") == "ok", f"Unexpected /health payload: {data!r}"
    print("GET /health -> 200 ok")


async def check_seeded_session(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("seed.txt", b"seeded content", filename="seed.txt", content_type="application/octet-stream")
    async with session.post(f"{api_url}/sessions", data=form) as response:
        assert response.status == 200, f"Seeded session create failed: {response.status}"
        data = await response.json()
        session_id = data["session_id"]
    print(f"Created seeded session: {session_id}")

    try:
        async with session.get(f"{api_url}/sessions/{session_id}/files/seed.txt") as response:
            assert response.status == 200, f"GET seed.txt failed: {response.status}"
            content = await response.read()
            assert content == b"seeded content", f"Unexpected seeded content: {content!r}"
        print(f"GET seed.txt -> {content!r}")
    finally:
        async with session.delete(f"{api_url}/sessions/{session_id}") as response:
            assert response.status == 204, f"DELETE seeded session failed: {response.status}"


async def check_file_lifecycle(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_session_deletion(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    session_id = await _new_session(session, api_url)
    async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
        assert response.status == 204, f"PUT failed: {response.status}"

    async with session.delete(f"{api_url}/sessions/{session_id}") as response:
        assert response.status == 204, f"DELETE session failed: {response.status}"
    print("DELETE session -> 204")

    async with session.get(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
        assert response.status == 404, f"Expected 404 after session deletion, got {response.status}"
    print("GET after delete -> 404 (as expected)")


async def check_execute_persistence(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
        assert response.status == 204, f"PUT hello.txt (fixture) failed: {response.status}"

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


async def check_execute_attachments(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_symlink_attachment_excluded(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    result = await _execute(
        session, api_url, "python", "import os\nos.symlink('/etc/passwd', 'leak')\nprint('linked')",
        session_id=session_id,
    )
    print(f"Execute symlink creation: output={result['output']!r} files={list(result['files'])}")
    assert "leak" not in result["files"], "a symlink was exposed as an execute attachment"

    async with session.get(f"{api_url}/sessions/{session_id}/files/leak") as response:
        assert response.status == 404, f"Expected 404 reading a symlink, got {response.status}"
    print("GET symlinked file -> 404 (symlink not followed, as expected)")


async def check_symlink_directory_escape_blocked(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_readonly_root_filesystem(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    result = await _execute(session, api_url, "bash", "touch /pwned.txt", session_id=session_id)
    print(f"Write outside /app: output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] != 0, "Writing outside the mounted /app directory unexpectedly succeeded"
    assert "read-only" in result["output"].lower(), f"Unexpected failure mode: {result['output']!r}"


async def check_disk_quota_enforcement(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Check that MAX_SESSION_SIZE stops both writers: executed code and the API itself.

    The probe deliberately fills its session to the byte quota and leaves it there, which is
    why every check gets its own session.
    """
    result = await _execute(session, api_url, "bash", "dd if=/dev/zero of=small.bin bs=1M count=1 2>&1", session_id=session_id)
    print(f"Disk quota probe (1MiB write): output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] == 0, f"A small write well under the quota unexpectedly failed: {result['output']!r}"

    probe_mib = cfg.max_session_size // (1024 * 1024) + 16
    result = await _execute(session, api_url, "bash", f"dd if=/dev/zero of=quota_probe.bin bs=1M count={probe_mib} 2>&1", session_id=session_id)
    print(f"Disk quota probe ({probe_mib}MiB write): output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] != 0, (
        "Writing well past --max-session-size from inside the container succeeded -- "
        "the session directory does not appear to be under an XFS project quota "
        "(check SESSION_QUOTA_MOUNTPOINT and that xfs_quota is usable by the API process)"
    )
    assert "no space" in result["output"].lower(), f"Unexpected failure mode, expected an ENOSPC error: {result['output']!r}"

    # The session now sits at its byte quota, so a host-mediated write has to be refused
    # too. The API keeps no byte accounting of its own - this 413 is the quota rejecting
    # the write, so it is the only check covering that path.
    upload_url = f"{api_url}/sessions/{session_id}/files/host_probe.bin"
    async with session.put(upload_url, data=b"x" * 65536) as response:
        body = await response.text()
    assert response.status == 413, (
        f"Expected 413 for a PUT into a session already at its byte quota, got {response.status}: {body}"
    )
    print(f"PUT into a session at its quota -> 413 {body}")

    async with session.get(upload_url) as response:
        assert response.status == 404, f"A rejected PUT left a file behind: {response.status}"
    print("GET the rejected upload -> 404 (as expected, nothing was left behind)")


async def check_inode_quota_enforcement(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    probe = cfg.max_session_entries + 256
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


async def check_rejected_execute_leaves_no_attachment(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_ephemeral_execute(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    result = await _execute(session, api_url, "javascript", "console.log('from ephemeral session')")
    print(f"Ephemeral /execute: output={result['output']!r} return_code={result['return_code']}")
    assert result["return_code"] == 0, f"Ephemeral execute failed: {result}"


async def check_javascript_module_styles(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_typescript(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


# The five languages that compile before they run, each through its own /executors/*.sh.
# `java` has to declare `Main`, because java.sh writes /tmp/Main.java and runs `java -cp /tmp Main`.
_COMPILED_HELLO: dict[str, str] = {
    "c": '#include <stdio.h>\nint main(void) { printf("harness-ok\\n"); return 0; }',
    "cpp": '#include <iostream>\nint main() { std::cout << "harness-ok" << std::endl; return 0; }',
    "java": 'public class Main { public static void main(String[] args) { System.out.println("harness-ok"); } }',
    "csharp": 'System.Console.WriteLine("harness-ok");',
    "rust": 'fn main() { println!("harness-ok"); }',
}

_COMPILED_BROKEN: dict[str, str] = {
    "c": "int main(void) { return no_such_symbol_here; }",
    "cpp": "int main() { std::cout << no_such_symbol_here; }",  # no <iostream> either
    "java": 'public class Main { public static void main(String[] args) { int x = "not an int"; } }',
    "csharp": 'int x = "not an int";',
    "rust": 'fn main() { let x: i32 = "not an int"; }',
}


async def check_compiled_languages(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Every compiled language runs a hello world and leaves the session untouched.

    The second half matters as much as the first: each executor compiles into /tmp, which is
    a per-run tmpfs, so a toolchain that starts writing into $HOME (the session mount) would
    return its build noise to the caller as attachments. That is the regression d786243 fixed
    for C# and 89dc6b3 fixed for TypeScript, and nothing has watched for it since.
    """
    failures = []
    for language, code in _COMPILED_HELLO.items():
        result = await _execute(session, api_url, language, code, session_id=session_id)
        output = _plain(result["output"])
        print(f"Execute {language} (hello world) -> return_code={result['return_code']} output={output!r} "
              f"files={sorted(result['files'])}")
        if result["return_code"] != 0 or "harness-ok" not in output:
            failures.append(f"{language}: return_code={result['return_code']} output={output!r}")
        elif result["files"]:
            failures.append(f"{language}: left files in the session: {sorted(result['files'])}")
    assert not failures, "Compiled languages that did not run cleanly:\n  " + "\n  ".join(failures)


async def check_compiled_language_errors(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A compile error must fail the run and surface the compiler's diagnostics.

    Every executor script runs under `set -e`, so a non-zero compiler exit has to abort the
    script rather than fall through to running a stale binary from a previous step.
    """
    failures = []
    for language, code in _COMPILED_BROKEN.items():
        result = await _execute(session, api_url, language, code, session_id=session_id)
        output = _plain(result["output"])
        print(f"Execute {language} (compile error) -> return_code={result['return_code']} "
              f"output={output[:120]!r}")
        if result["return_code"] == 0:
            failures.append(f"{language}: a compile error exited 0")
        elif "error" not in output.lower():
            failures.append(f"{language}: no diagnostics in the output: {output!r}")
    assert not failures, "Compile errors that were not reported properly:\n  " + "\n  ".join(failures)


async def check_typescript_rejects_top_level_await(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """typescript runs as CommonJS, so top-level await is not available to it (README).

    `javascript` accepts the same code - check_javascript_module_styles covers that - and the
    asymmetry is the documented trade: --input-type=module would buy top-level await at the
    cost of `require`.
    """
    code = "console.log('ok', await Promise.resolve('x'));"
    result = await _execute(session, api_url, "typescript", code, session_id=session_id)
    output = _plain(result["output"])
    print(f"Execute typescript (top-level await) -> return_code={result['return_code']} output={output[:200]!r}")
    assert result["return_code"] != 0, (
        f"typescript accepted top-level await, which the README documents as unsupported: {output!r}"
    )
    assert "await" in output.lower(), f"Expected a diagnostic mentioning await, got: {output!r}"


async def check_directory_listing(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    # This check owns everything it asserts on: a plain file, a symlink to a file and a
    # symlink to a directory, all created here rather than left behind by earlier checks.
    async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
        assert response.status == 204, f"PUT hello.txt (listing fixture) failed: {response.status}"
    fixtures = await _execute(
        session, api_url, "python",
        "import os\nos.symlink('/etc/passwd', 'leak')\nos.symlink('/tmp', 'escapedir')\nprint('linked')",
        session_id=session_id,
    )
    print(f"Listing fixtures: output={fixtures['output']!r} files={list(fixtures['files'])}")

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


async def check_result_attachment_cap(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    total = cfg.max_result_attachments + 50
    result = await _execute(
        session, api_url, "bash",
        f"for i in $(seq 1 {total}); do printf 'content %s' \"$i\" > \"out_$i.txt\"; done",
        session_id=session_id,
    )
    omitted = result["omitted_files"]
    print(f"Created {total} files -> {len(result['files'])} attachments, {len(omitted)} omitted")

    assert result["return_code"] == 0, f"Creating {total} files failed: {result['output']!r}"
    assert len(result["files"]) == cfg.max_result_attachments, (
        f"Expected exactly {cfg.max_result_attachments} attachments, got {len(result['files'])} -- "
        "the server's MAX_RESULT_ATTACHMENTS probably differs from --max-result-attachments"
    )
    assert len(omitted) == total - cfg.max_result_attachments, (
        f"Expected {total - cfg.max_result_attachments} omitted files, got {len(omitted)}"
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


async def check_change_detection_stability(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_home_directory_noise_excluded(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


# Names executed code can legally create that a Content-Disposition header cannot carry in a
# plain `filename` parameter. Quotes and newlines would terminate or inject header syntax; the
# slash would be stripped by a conforming client (RFC 6266); the rest are simply non-ASCII or
# reserved. All of them have to survive in the RFC 5987 `filename*` parameter instead.
_AWKWARD_NAMES: tuple[str, ...] = (
    "sp ace.txt",
    "café.txt",
    "日本語.txt",
    'we"ird.txt',
    "per%cent.txt",
    "semi;colon.txt",
    "quo'te.txt",
    "e=quals.txt",
    "new\nline.txt",
    "nested/dir/deep.txt",
)

# A newline cannot be carried in a URL path, so that one name is exercised as an attachment
# only - which is the half that matters, since it is the header it must not be able to break.
_RETRIEVABLE_AWKWARD_NAMES = tuple(name for name in _AWKWARD_NAMES if "\n" not in name)


async def check_awkward_result_filenames(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A result attachment's sub_path survives the response header exactly.

    This is what `_content_disposition` in file_helpers.py exists for, and nothing has tested
    it beyond plain ASCII. `part.filename` prefers the RFC 5987 `filename*` parameter, so it
    must come back byte-identical; the flattened ASCII `filename` fallback must simultaneously
    be free of anything that could alter the header.
    """
    # Written with Python rather than the shell so the names need no quoting gymnastics.
    literals = ", ".join(repr(name) for name in _AWKWARD_NAMES)
    code = (
        "import pathlib\n"
        f"names = [{literals}]\n"
        "for name in names:\n"
        "    p = pathlib.Path(name)\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text('content of ' + name)\n"
        "print('wrote', len(names))"
    )
    result = await _execute(session, api_url, "python", code, session_id=session_id)
    assert result["return_code"] == 0, f"Creating the awkward-named files failed: {result['output']!r}"

    returned = set(result["files"])
    print(f"Awkward names returned as attachments: {sorted(returned)}")
    missing = [name for name in _AWKWARD_NAMES if name not in returned]
    assert not missing, (
        f"These names did not round-trip through Content-Disposition: {missing} -- "
        f"got {sorted(returned)}"
    )

    for name in _AWKWARD_NAMES:
        expected = f"content of {name}".encode()
        assert result["files"][name] == expected, (
            f"Attachment {name!r} carried the wrong bytes: {result['files'][name]!r}"
        )
    print("All awkward names round-tripped exactly, with matching content (as expected)")

    # And each one is still addressable individually through the files API. The sub_path has
    # to be percent-encoded into the URL - leaving a literal '%' in would make yarl read
    # "per%cent.txt" as an escape sequence - so the request is built pre-encoded.
    for name in _RETRIEVABLE_AWKWARD_NAMES:
        url = URL(f"{api_url}/sessions/{session_id}/files/{quote(name, safe='/')}", encoded=True)
        async with session.get(url) as response:
            assert response.status == 200, f"GET {name!r} failed: {response.status}"
            assert (await response.read()) == f"content of {name}".encode(), f"Wrong bytes for {name!r}"
    print("All awkward names are individually retrievable through the files API (as expected)")


async def check_non_utf8_filename_omitted(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A filename that is not valid UTF-8 is named in omitted_files, never a 500.

    The name cannot go in a `filename*` parameter (percent-encoding it requires encoding it as
    UTF-8 first), so the attachment is dropped - but the run succeeded, so the response must
    still describe what changed. The listing is where such a file stays discoverable.
    """
    result = await _execute(
        session, api_url, "bash",
        r"printf 'x' > $'bad\xff\xfe.txt' && echo readable > fine.txt && echo done",
        session_id=session_id,
    )
    print(f"Non-UTF-8 filename run: return_code={result['return_code']} "
          f"files={sorted(result['files'])} omitted={result['omitted_files']!r}")

    assert result["return_code"] == 0, f"Creating a non-UTF-8 filename failed: {result['output']!r}"
    # surrogateescape is how the server decoded the raw bytes, and json round-trips it exactly.
    bad_name = "bad\udcff\udcfe.txt"
    assert bad_name in result["omitted_files"], (
        f"A non-UTF-8 filename must be named in omitted_files, got {result['omitted_files']!r}"
    )
    assert bad_name not in result["files"], "A non-UTF-8 filename must not be sent as an attachment"
    assert result["files"].get("fine.txt") == b"readable\n", (
        f"An unreadable sibling must not disturb the other attachments: {sorted(result['files'])}"
    )
    print(f"Non-UTF-8 filename -> omitted_files (as expected), siblings unaffected")

    # Unlike execution results, the listing hides nothing - it is the only way to see it.
    async with session.get(f"{api_url}/sessions/{session_id}/files/") as response:
        assert response.status == 200, f"Expected 200 listing the session root, got {response.status}"
        names = [entry["name"] for entry in (await response.json())["entries"]]
    assert bad_name in names, f"The listing must still show a non-UTF-8 name, got {names!r}"
    print("The directory listing still shows the non-UTF-8 name (as expected)")


async def check_deleted_files_not_truncated(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """deleted_files is never capped, unlike the attachment list.

    MAX_RESULT_ATTACHMENTS bounds what the response *carries*; a deletion carries nothing, so
    there is no reason to truncate it and the README says it is not.
    """
    total = cfg.max_result_attachments + 50
    result = await _execute(
        session, api_url, "bash", f"for i in $(seq 1 {total}); do echo x > \"gone_$i.txt\"; done",
        session_id=session_id,
    )
    assert result["return_code"] == 0, f"Creating {total} files failed: {result['output']!r}"

    result = await _execute(session, api_url, "bash", "rm gone_*.txt", session_id=session_id)
    print(f"Deleted {total} files -> {len(result['deleted_files'])} named in deleted_files")
    assert result["return_code"] == 0, f"Deleting the files failed: {result['output']!r}"
    assert len(result["deleted_files"]) == total, (
        f"deleted_files was truncated: expected {total} names, got {len(result['deleted_files'])}"
    )
    assert set(result["deleted_files"]) == {f"gone_{i}.txt" for i in range(1, total + 1)}, (
        "deleted_files did not name exactly the files that were removed"
    )
    print(f"All {total} deletions reported, past the {cfg.max_result_attachments} attachment cap (as expected)")


async def check_change_detection_adversarial(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Changes that a naive (size, mtime) stamp would miss are still reported.

    Change detection compares (inode, size, mtime, ctime) rather than hashing, which is what
    makes these three cases interesting: each defeats one component and has to be caught by
    another. ctime is the backstop, and it cannot be forged without privileges the container
    does not have.
    """
    async with session.put(f"{api_url}/sessions/{session_id}/files/probe.txt", data=b"AAAA") as response:
        assert response.status == 204, f"PUT probe.txt failed: {response.status}"

    # Same size, and mtime deliberately restored afterwards: only ctime moved.
    result = await _execute(
        session, api_url, "python",
        "import os\nst = os.stat('probe.txt')\n"
        "open('probe.txt', 'w').write('BBBB')\n"
        "os.utime('probe.txt', ns=(st.st_atime_ns, st.st_mtime_ns))\n"
        "print('rewritten with mtime restored')",
        session_id=session_id,
    )
    print(f"Same-size rewrite with mtime restored -> files={sorted(result['files'])}")
    assert result["files"].get("probe.txt") == b"BBBB", (
        "A same-size rewrite with a restored mtime was not detected -- the ctime component of "
        f"the change signature is not doing its job: {sorted(result['files'])}"
    )

    # Delete and recreate with identical content, size and mtime: only the inode moved.
    result = await _execute(
        session, api_url, "python",
        "import os\nst = os.stat('probe.txt')\nos.remove('probe.txt')\n"
        "open('probe.txt', 'w').write('BBBB')\n"
        "os.utime('probe.txt', ns=(st.st_atime_ns, st.st_mtime_ns))\n"
        "print('recreated identically')",
        session_id=session_id,
    )
    print(f"Delete-and-recreate with identical content/size/mtime -> files={sorted(result['files'])} "
          f"deleted={result['deleted_files']}")
    assert "probe.txt" in result["files"], (
        "A delete-and-recreate with identical metadata was not detected -- the inode component "
        f"of the change signature is not doing its job: {sorted(result['files'])}"
    )
    assert not result["deleted_files"], (
        f"A file that exists again at collect time must not be reported deleted: {result['deleted_files']}"
    )

    # A rename is one deletion plus one creation, carrying the original bytes.
    result = await _execute(session, api_url, "bash", "mv probe.txt renamed.txt", session_id=session_id)
    print(f"Rename -> files={sorted(result['files'])} deleted={result['deleted_files']}")
    assert result["deleted_files"] == ["probe.txt"], f"Rename source not reported deleted: {result['deleted_files']}"
    assert result["files"].get("renamed.txt") == b"BBBB", (
        f"Rename destination not returned with the original bytes: {sorted(result['files'])}"
    )
    print("All three adversarial change cases detected (as expected)")


async def check_hidden_file_vs_hidden_directory(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Only hidden *directories* are excluded from results; a hidden file is a real result.

    The exclusion exists because the session directory doubles as $HOME and package managers
    fill it with dot-directories. A single dotfile that executed code wrote deliberately is
    not that, so it must come back.
    """
    result = await _execute(
        session, api_url, "bash",
        "echo secret > .env && mkdir -p .cache && echo noise > .cache/blob && echo plain > visible.txt",
        session_id=session_id,
    )
    print(f"Hidden file vs hidden directory -> files={sorted(result['files'])}")
    assert result["files"].get(".env") == b"secret\n", (
        f"A hidden file at the session root must be returned as a result: {sorted(result['files'])}"
    )
    assert not any(name.startswith(".cache/") for name in result["files"]), (
        f"A hidden directory's contents must stay out of the results: {sorted(result['files'])}"
    )
    assert result["files"].get("visible.txt") == b"plain\n", f"Missing the plain file: {sorted(result['files'])}"
    print("Hidden file returned, hidden directory excluded (as expected)")


async def _post_raw(
        session: aiohttp.ClientSession, url: str, body: bytes, content_type: str,
) -> tuple[int, str]:
    """POST a hand-rolled body, for shapes aiohttp.FormData will not produce."""
    async with session.post(url, data=body, headers={aiohttp.hdrs.CONTENT_TYPE: content_type}) as response:
        return response.status, (await response.text())[:200]


_RAW_BOUNDARY = "harnessboundaryLQ8x3f"


def _raw_multipart(parts: Sequence[tuple[str, str | None, bytes]]) -> tuple[bytes, str]:
    """Build a multipart body by hand, as (body, content_type).

    aiohttp's FormData cannot express two of the shapes worth testing: it substitutes the
    field name when a bytes value is given no filename, and it percent-encodes a filename
    into the RFC 5987 parameter, which turns "../escape.txt" into the harmless literal name
    "..%2Fescape.txt". Both are sensible client behaviour and both hide what the server would
    do with the raw thing, so these bodies are assembled directly.
    """
    chunks = []
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(
            f"--{_RAW_BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
            + content + b"\r\n"
        )
    chunks.append(f"--{_RAW_BOUNDARY}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={_RAW_BOUNDARY}"


async def check_execute_field_validation(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Every malformed /execute request is rejected as invalid input, not as a server error.

    The README's error table promises 400 for invalid input, and each of these is invalid
    input. They are grouped because they share one property worth checking together: a
    rejected request must never start a container.
    """
    url = f"{api_url}/sessions/{session_id}/execute"

    def form(**parts) -> aiohttp.FormData:
        data = aiohttp.FormData(default_to_multipart=True)
        for name, value in parts.items():
            data.add_field(name, value)
        return data

    cases: list[tuple[str, aiohttp.FormData]] = []

    unknown_field = form(language="python", code="print(1)")
    unknown_field.add_field("surprise", "unexpected")
    cases.append(("an unknown multipart field", unknown_field))

    cases.append(("a missing language field", form(code="print(1)")))
    cases.append(("a missing code field", form(language="python")))
    cases.append(("a NUL byte in code", form(language="python", code="print('a\x00b')")))

    failures = []
    for description, data in cases:
        async with session.post(url, data=data) as response:
            status, body = response.status, (await response.text())[:160]
        print(f"Execute with {description} -> {status} {body!r}")
        if status != 400:
            failures.append(f"{description}: expected 400, got {status} ({body!r})")

    # These two have to go on the wire by hand: FormData would substitute the field name for
    # the missing filename, and would percent-encode the traversal into a harmless literal.
    raw_cases = [
        ("an attachments part with no filename",
         [("language", None, b"python"), ("code", None, b"print(1)"),
          ("attachments", None, b"no filename on this part")]),
        ("an attachment escaping the session root",
         [("language", None, b"python"), ("code", None, b"print(1)"),
          ("attachments", "../escape.txt", b"escape")]),
        ("an attachment escaping through a subdirectory",
         [("language", None, b"python"), ("code", None, b"print(1)"),
          ("attachments", "a/../../outside.txt", b"escape")]),
    ]
    for description, parts in raw_cases:
        body_bytes, content_type = _raw_multipart(parts)
        status, body = await _post_raw(session, url, body_bytes, content_type)
        print(f"Execute with {description} -> {status} {body!r}")
        if status != 400:
            failures.append(f"{description}: expected 400, got {status} ({body!r})")

    assert not failures, (
        "Malformed /execute requests that were not rejected with 400:\n  " + "\n  ".join(failures)
    )
    print("All malformed /execute requests -> 400 (as expected)")


async def check_execute_oversized_code(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Code past MAX_CODE_LENGTH is refused, and the attachment sent with it is discarded.

    The size cap has to fire while the multipart stream is still being read, which is exactly
    when an earlier attachment part has already been staged - so this is the case that proves
    the staging cleanup runs on the size-limit path too, not just on validation failures.
    """
    data = aiohttp.FormData(default_to_multipart=True)
    data.add_field("attachments", b"should not persist", filename="oversized_probe.txt",
                   content_type="application/octet-stream")
    data.add_field("language", "python")
    data.add_field("code", "#" + "x" * (cfg.max_code_length + 4096))

    async with session.post(f"{api_url}/sessions/{session_id}/execute", data=data) as response:
        status, body = response.status, (await response.text())[:160]
    print(f"Execute with code past MAX_CODE_LENGTH -> {status} {body!r}")
    assert status == 413, (
        f"Expected 413 for code past --max-code-length ({cfg.max_code_length}), got {status}: {body!r} -- "
        "the server's MAX_CODE_LENGTH may differ from --max-code-length"
    )

    async with session.get(f"{api_url}/sessions/{session_id}/files/oversized_probe.txt") as response:
        assert response.status == 404, (
            f"The attachment from an oversized request was left behind: {response.status}"
        )
    print("GET the attachment from the oversized request -> 404 (as expected, nothing left behind)")


async def check_execute_malformed_body(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A body that is not usable multipart is invalid input, so it must be a 400.

    Neither shape reaches any of the API's own validation: both blow up inside aiohttp's
    multipart reader while the request is still being parsed. That is what makes them worth
    checking - an uncaught parser exception becomes a 500, which tells a client that the
    server is broken rather than that the request was.
    """
    url = f"{api_url}/sessions/{session_id}/execute"
    failures = []

    status, body = await _post_raw(session, url, b'{"language": "python", "code": "print(1)"}',
                                   "application/json")
    print(f"Execute with a JSON body -> {status} {body!r}")
    if status != 400:
        failures.append(f"a non-multipart (application/json) body: expected 400, got {status} ({body!r})")

    status, body = await _post_raw(session, url, b"--nope\r\nContent-Disposition: form-data\r\n\r\nx\r\n--nope--\r\n",
                                   "multipart/form-data")
    print(f"Execute with multipart but no boundary parameter -> {status} {body!r}")
    if status != 400:
        failures.append(f"multipart/form-data with no boundary: expected 400, got {status} ({body!r})")

    assert not failures, (
        "Unusable /execute bodies that were not rejected with 400:\n  " + "\n  ".join(failures)
        + "\n  (a 500 here means the multipart parser's exception escaped the handler's except chain)"
    )


async def check_execute_oversized_language(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """An absurdly long `language` is an unsupported language, so it is a 400.

    The field is read through a 64-byte cap, which is a sensible guard, but overrunning it
    describes the same condition as any other unknown language: the README's error table
    calls that 400 invalid input, not a request-size problem.
    """
    data = aiohttp.FormData(default_to_multipart=True)
    data.add_field("language", "p" * 512)
    data.add_field("code", "print(1)")
    async with session.post(f"{api_url}/sessions/{session_id}/execute", data=data) as response:
        status, body = response.status, (await response.text())[:160]
    print(f"Execute with a 512-byte language field -> {status} {body!r}")
    assert status == 400, (
        f"Expected 400 for an over-long language (it names no supported language), got {status}: {body!r} -- "
        "the 64-byte read cap fires before the language is validated, turning a bad-language "
        "error into a request-size error"
    )


async def check_nul_byte_in_path(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A NUL byte in a sub_path is an invalid path, so it is a 400.

    Code is already checked for NUL bytes before it is passed to the container; a path is not,
    and a NUL cannot survive the syscall that would open it. The README's error table calls an
    invalid path 400.
    """
    failures = []

    url = URL(f"{api_url}/sessions/{session_id}/files/bad%00name.txt", encoded=True)
    async with session.put(url, data=b"x") as response:
        status, body = response.status, (await response.text())[:160]
    print(f"PUT a path containing a NUL byte -> {status} {body!r}")
    if status != 400:
        failures.append(f"PUT with a NUL in the path: expected 400, got {status} ({body!r})")

    async with session.get(url) as response:
        status, body = response.status, (await response.text())[:160]
    print(f"GET a path containing a NUL byte -> {status} {body!r}")
    if status not in (400, 404):
        failures.append(f"GET with a NUL in the path: expected 400 or 404, got {status} ({body!r})")

    # Sent raw: aiohttp refuses to serialise a NUL into a header client-side, which is the
    # right call for a client but means FormData cannot pose the question to the server.
    body_bytes, content_type = _raw_multipart([
        ("language", None, b"python"), ("code", None, b"print(1)"),
        ("attachments", "bad\x00name.txt", b"x"),
    ])
    status, body = await _post_raw(
        session, f"{api_url}/sessions/{session_id}/execute", body_bytes, content_type)
    print(f"Execute with a NUL in an attachment filename -> {status} {body!r}")
    if status != 400:
        failures.append(f"an attachment filename with a NUL: expected 400, got {status} ({body!r})")

    assert not failures, (
        "NUL bytes in a path were not rejected as invalid input:\n  " + "\n  ".join(failures)
        + "\n  (a 500 means os.open's ValueError escaped the handler, which catches only OSError)"
    )


async def check_session_seed_validation(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """A rejected seeded-session create leaves no session behind.

    Seeding happens after the session already exists, so every failure path has to undo that
    creation - otherwise a client that sends a bad seed burns a MAX_SESSIONS slot it never got
    to use, and only the inactivity sweep would ever give it back.
    """
    async with session.get(f"{api_url}/health") as response:
        assert response.status == 200, "server went away"

    failures = []
    # Raw bodies again: FormData cannot omit a filename, nor send an un-encoded traversal.
    for description, filename in (("a seed part with no filename", None),
                                  ("a seed filename escaping the root", "../escape.txt")):
        body_bytes, content_type = _raw_multipart([("seed", filename, b"seed content")])
        status, body = await _post_raw(session, f"{api_url}/sessions", body_bytes, content_type)
        print(f"POST /sessions with {description} -> {status} {body!r}")
        if status != 400:
            failures.append(f"{description}: expected 400, got {status} ({body!r})")

    # The slots those attempts would have consumed must be free again: if they leaked, this
    # create eventually starts failing with 503 rather than returning an id.
    probe = await _new_session(session, api_url)
    async with session.delete(f"{api_url}/sessions/{probe}") as response:
        assert response.status == 204, f"cleanup DELETE failed: {response.status}"
    print("A session can still be created afterwards (as expected, no slot leaked)")

    assert not failures, (
        "Rejected seeded-session creates that did not return 400:\n  " + "\n  ".join(failures)
    )


async def check_error_cases(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
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


async def check_put_path_edges(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """PUT creates what it can and refuses what it cannot, without collateral damage."""
    files = f"{api_url}/sessions/{session_id}/files"

    async with session.put(f"{files}/deep/nested/dirs/file.txt", data=b"nested") as response:
        assert response.status == 204, f"PUT creating parent directories failed: {response.status}"
    async with session.get(f"{files}/deep/nested/dirs/file.txt") as response:
        assert response.status == 200 and (await response.read()) == b"nested", "nested PUT did not land"
    print("PUT creating missing parent directories -> 204 (as expected)")

    async with session.put(f"{files}/deep", data=b"clobber") as response:
        assert response.status == 400, f"Expected 400 writing onto a directory, got {response.status}"
    async with session.get(f"{files}/deep/nested/dirs/file.txt") as response:
        assert response.status == 200, "A refused PUT onto a directory damaged its contents"
    print("PUT onto an existing directory -> 400, directory intact (as expected)")

    async with session.put(f"{files}/plain.txt", data=b"original") as response:
        assert response.status == 204, f"PUT plain.txt failed: {response.status}"
    async with session.put(f"{files}/plain.txt/child.txt", data=b"through a file") as response:
        assert response.status == 400, (
            f"Expected 400 writing through a regular file, got {response.status}"
        )
    async with session.get(f"{files}/plain.txt") as response:
        assert (await response.read()) == b"original", "A refused PUT damaged the file in the path"
    print("PUT through a regular file -> 400, that file intact (as expected)")

    # A zero-byte file is a legitimate file, not a missing one.
    async with session.put(f"{files}/empty.txt", data=b"") as response:
        assert response.status == 204, f"Zero-byte PUT failed: {response.status}"
    async with session.get(f"{files}/empty.txt") as response:
        assert response.status == 200, f"Expected 200 reading a zero-byte file, got {response.status}"
        assert (await response.read()) == b"", "A zero-byte file read back non-empty"
    print("Zero-byte PUT -> 204, reads back empty (as expected)")


async def check_put_replaces_symlink(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Writing to a name held by a symlink replaces the link, never follows it.

    Following it would write through to wherever the link points, which for a link created by
    executed code is an arbitrary path outside the session. Replacing it is both the safe
    behaviour and the one that keeps PUT's contract - the bytes end up at the sub_path asked for.
    """
    files = f"{api_url}/sessions/{session_id}/files"
    result = await _execute(
        session, api_url, "python",
        "import os\nos.symlink('/etc/passwd', 'link.txt')\nprint('linked')",
        session_id=session_id,
    )
    assert result["return_code"] == 0, f"Creating the symlink failed: {result['output']!r}"

    async with session.put(f"{files}/link.txt", data=b"replaced the link") as response:
        assert response.status == 204, f"Expected 204 writing onto a symlink, got {response.status}"

    async with session.get(f"{files}/link.txt") as response:
        assert response.status == 200, f"Expected 200 reading the replacement, got {response.status}"
        assert (await response.read()) == b"replaced the link", "The replacement did not take"

    async with session.get(f"{files}/") as response:
        entries = {entry["name"]: entry for entry in (await response.json())["entries"]}
    assert entries["link.txt"]["type"] == "file", (
        f"The symlink must have been replaced by a regular file, got {entries['link.txt']['type']!r} -- "
        "a PUT that followed the link would have left the symlink in place"
    )
    print("PUT onto a symlink -> 204, link replaced by a regular file (as expected)")


async def check_delete_path_edges(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """DELETE removes files and symlinks, and refuses directories and the root."""
    files = f"{api_url}/sessions/{session_id}/files"

    result = await _execute(
        session, api_url, "python",
        "import os\nos.mkdir('adir')\nopen('adir/inside.txt','w').write('kept')\n"
        "os.symlink('/etc/passwd', 'alink')\nprint('set up')",
        session_id=session_id,
    )
    assert result["return_code"] == 0, f"Setting up the delete fixtures failed: {result['output']!r}"

    async with session.delete(f"{files}/adir") as response:
        assert response.status == 404, (
            f"Expected 404 deleting a directory (the files API deletes files), got {response.status}"
        )
    async with session.get(f"{files}/adir/inside.txt") as response:
        assert response.status == 200, "A refused directory DELETE removed its contents anyway"
    print("DELETE on a directory -> 404, contents intact (as expected)")

    # A symlink is removable - unlinking it is the only way to get rid of one, and it does
    # not touch whatever it points at.
    async with session.delete(f"{files}/alink") as response:
        assert response.status == 204, f"Expected 204 deleting a symlink, got {response.status}"
    async with session.get(f"{files}/") as response:
        names = [entry["name"] for entry in (await response.json())["entries"]]
    assert "alink" not in names, f"The symlink was not removed: {names}"
    print("DELETE on a symlink -> 204, link removed (as expected)")

    async with session.delete(f"{files}/") as response:
        assert response.status in (400, 404), (
            f"Expected the session root to be undeletable through the files API, got {response.status}"
        )
    async with session.get(f"{files}/adir/inside.txt") as response:
        assert response.status == 200, "Deleting the root through the files API wiped the session"
    print("DELETE on the session root -> refused, session intact (as expected)")


async def check_special_file_types(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A FIFO or a socket is listed as "other", is never an attachment, and is not readable.

    Neither has bytes to serve, so the files API has to decline rather than block on a FIFO
    with no writer or fail on a socket that cannot be opened at all. Declining means the same
    404 an absent file gets - the caller asked for file content and there is none.
    """
    files = f"{api_url}/sessions/{session_id}/files"
    result = await _execute(
        session, api_url, "python",
        "import os, socket\nos.mkfifo('a_fifo')\n"
        "s = socket.socket(socket.AF_UNIX)\ns.bind('a_socket')\n"
        "open('normal.txt','w').write('normal')\nprint('made special files')",
        session_id=session_id,
    )
    assert result["return_code"] == 0, f"Creating the special files failed: {result['output']!r}"
    assert sorted(result["files"]) == ["normal.txt"], (
        f"Only the regular file may be returned as an attachment, got {sorted(result['files'])}"
    )
    print(f"FIFO and socket excluded from attachments (got {sorted(result['files'])}, as expected)")

    async with session.get(f"{files}/") as response:
        entries = {entry["name"]: entry for entry in (await response.json())["entries"]}
    for name in ("a_fifo", "a_socket"):
        assert entries[name]["type"] == "other", (
            f"{name} should be listed as \"other\", got {entries[name]['type']!r}"
        )
    print("FIFO and socket both listed as type \"other\" (as expected)")

    failures = []
    for name in ("a_fifo", "a_socket"):
        async with session.get(f"{files}/{name}") as response:
            status, body = response.status, (await response.text())[:120]
        print(f"GET the {name} -> {status} {body!r}")
        if status != 404:
            failures.append(f"GET {name}: expected 404, got {status} ({body!r})")
    assert not failures, (
        "Unreadable special files that did not return 404:\n  " + "\n  ".join(failures)
        + "\n  (a 500 means the open(2) error was not one of the errnos the handler maps to "
          "not-found: a socket gives ENXIO, where a FIFO opens fine and fails the stat check)"
    )


async def check_sub_path_containment(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Odd-looking sub_paths either resolve inside the session or are refused. Never outside.

    Normalisation is lexical, so several of these are legal relative names rather than attacks:
    a directory called "...." is a perfectly good directory, a backslash is an ordinary filename
    character, and a leading "/" is stripped rather than rejected - so "//etc/passwd" addresses
    <session>/etc/passwd, not the real one. What matters is not which of 204/400 comes back but
    that the bytes never land above the session root, so each probe's exact resting place is
    pinned here.
    """
    files = f"{api_url}/sessions/{session_id}/files"
    # (URL-encoded probe, the sub_path it must resolve to, or None if it must be refused)
    probes: tuple[tuple[str, str | None], ...] = (
        ("%2e%2e/outside.txt", None),        # ..
        ("a/%2e%2e/%2e%2e/out.txt", None),   # a/../..
        ("%2e", None),                       # .
        ("%2f%2fetc/passwd", "etc/passwd"),  # a leading slash is stripped, not rejected
        ("....//x.txt", "..../x.txt"),
        ("%2e%2e%2e/y.txt", ".../y.txt"),
        ("back%5cslash.txt", "back\\slash.txt"),
        ("a//b.txt", "a/b.txt"),
        ("foo/%2e/bar.txt", "foo/bar.txt"),
    )

    failures = []
    for probe, expected in probes:
        async with session.put(URL(f"{files}/{probe}", encoded=True), data=b"probe") as response:
            status = response.status
        verdict = "refused" if expected is None else f"-> {expected!r}"
        print(f"PUT {probe!r} -> {status} ({verdict} expected)")
        if expected is None and status != 400:
            failures.append(f"{probe!r}: expected 400, got {status}")
        elif expected is not None and status != 204:
            failures.append(f"{probe!r}: expected 204 writing to {expected!r}, got {status}")
    assert not failures, "sub_path probes with the wrong status:\n  " + "\n  ".join(failures)

    # Every accepted probe must be exactly where it was supposed to land, and the session must
    # hold nothing else: a probe that escaped would be missing from here, not merely elsewhere.
    result = await _execute(session, api_url, "bash", "find . -type f | sort", session_id=session_id)
    found = {line.removeprefix("./") for line in _plain(result["output"]).split("\n") if line.strip()}
    expected_paths = {expected for _, expected in probes if expected is not None}
    print(f"Files in the session after the probes: {sorted(found)}")
    assert found == expected_paths, (
        f"The accepted probes did not land where they should have.\n"
        f"  expected: {sorted(expected_paths)}\n  found:    {sorted(found)}"
    )
    print(f"All {len(expected_paths)} accepted sub_paths resolved inside the session, exactly as expected")


async def check_root_listing_forms(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """The session root lists as a directory, and a missing path is a 404 either way."""
    async with session.get(f"{api_url}/sessions/{session_id}/files/") as response:
        assert response.status == 200, f"Expected 200 listing the root with a trailing slash, got {response.status}"
        listing = await response.json()
    assert listing["path"] == "", f"Expected an empty path for the root listing, got {listing['path']!r}"
    print("GET /files/ -> 200 listing (as expected)")

    # Without the trailing slash the request does not name the files resource at all, so a
    # 404 is right - but it is aiohttp's, not the API's, so it has no JSON envelope.
    async with session.get(f"{api_url}/sessions/{session_id}/files") as response:
        assert response.status == 404, (
            f"Expected 404 for /files with no trailing slash, got {response.status}"
        )
    print("GET /files (no trailing slash) -> 404 (as expected)")

    async with session.get(f"{api_url}/sessions/{session_id}/files/no/such/path.txt") as response:
        assert response.status == 404, f"Expected 404 for a missing nested path, got {response.status}"
    print("GET a missing nested path -> 404 (as expected)")


async def check_interrupted_put_leaves_original(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A PUT that dies mid-body leaves the previous file untouched and no debris behind.

    The body streams to a hidden temp file which is only renamed into place once it is whole,
    so an interrupted upload has nothing to commit. The temp file is dot-prefixed but is a
    regular file, and only hidden *directories* are excluded from results - so one left behind
    would show up as an attachment on the next run.
    """
    files = f"{api_url}/sessions/{session_id}/files"
    async with session.put(f"{files}/precious.txt", data=b"original bytes") as response:
        assert response.status == 204, f"PUT precious.txt failed: {response.status}"

    async def slow_body():
        yield b"x" * 8192
        await asyncio.sleep(30)  # cancelled long before this elapses
        yield b"never sent"

    upload = asyncio.create_task(
        session.put(f"{files}/precious.txt", data=slow_body()).__aenter__()
    )
    await asyncio.sleep(1.0)
    upload.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await upload
    print("Interrupted a PUT part-way through its body")

    async with session.get(f"{files}/precious.txt") as response:
        assert response.status == 200, f"The original file vanished: {response.status}"
        content = await response.read()
    assert content == b"original bytes", (
        f"An interrupted PUT replaced or truncated the original file: {content!r}"
    )
    print("The original file is byte-identical after the interrupted PUT (as expected)")

    result = await _execute(session, api_url, "bash", "true", session_id=session_id)
    debris = [name for name in result["files"] if name.startswith(".code_executor_upload_")]
    assert not debris, f"An interrupted PUT left its staging file behind: {debris}"
    async with session.get(f"{files}/") as response:
        names = [entry["name"] for entry in (await response.json())["entries"]]
    assert not [n for n in names if n.startswith(".code_executor_upload_")], (
        f"An interrupted PUT left a staging file in the session: {names}"
    )
    print("No staging file left behind (as expected)")


async def check_memory_limit(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Allocating past MAX_MEMORY kills the run promptly; it does not hang until the timeout.

    Swap is disabled alongside the limit (--memory-swap equals --memory), which is what makes
    this fail fast instead of thrashing. A run that merely timed out would still be contained,
    but it would hold an execution slot for EXECUTION_TIMEOUT and hide the real cause.
    """
    result = await _execute(
        session, api_url, "python",
        "buf = bytearray()\n"
        "while True:\n"
        "    buf.extend(b'x' * (16 * 1024 * 1024))\n"
        "    print(len(buf) // (1024 * 1024), 'MiB', flush=True)",
        session_id=session_id,
    )
    output = _plain(result["output"])
    reached = [int(line.split()[0]) for line in output.split("\n") if line.strip().endswith("MiB")]
    print(f"Memory probe: return_code={result['return_code']} timed_out={result['timed_out']} "
          f"high water {max(reached, default=0)}MiB")

    assert result["return_code"] != 0, (
        f"An unbounded allocation completed successfully, so MAX_MEMORY is not being enforced: {output[-200:]!r}"
    )
    assert not result["timed_out"], (
        "An unbounded allocation ran until EXECUTION_TIMEOUT instead of being killed -- with "
        "--memory-swap equal to --memory it should die as soon as it hits the cap"
    )
    print("Allocating past MAX_MEMORY -> killed, not timed out (as expected)")


async def check_pids_limit(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Process creation is capped, and the session still works afterwards.

    The cap is what stops a fork bomb from taking the host down with it. Equally important is
    the second half: hitting it must be an ordinary failed run, not something that leaves the
    session or the API wedged.
    """
    attempts = cfg.container_pids_limit * 4
    result = await _execute(
        session, api_url, "python",
        "import subprocess\n"
        "children = []\n"
        "try:\n"
        f"    for _ in range({attempts}):\n"
        "        children.append(subprocess.Popen(['sleep', '60']))\n"
        "except Exception as exc:\n"
        "    print('blocked after', len(children), type(exc).__name__)\n"
        "else:\n"
        "    print('spawned all', len(children))\n"
        "finally:\n"
        "    for child in children:\n"
        "        child.kill()",
        session_id=session_id,
    )
    output = _plain(result["output"])
    print(f"Pids probe: return_code={result['return_code']} output={output.strip()[:160]!r}")
    assert "blocked after" in output, (
        f"Spawning {attempts} processes was not refused, so CONTAINER_PIDS_LIMIT "
        f"(--container-pids-limit {cfg.container_pids_limit}) is not being enforced: {output!r}"
    )
    spawned = int(output.split("blocked after")[1].split()[0])
    assert spawned <= cfg.container_pids_limit, (
        f"Process creation was capped at {spawned}, above the configured limit of "
        f"{cfg.container_pids_limit}"
    )
    print(f"Process creation blocked after {spawned} children, at or under the limit (as expected)")

    result = await _execute(session, api_url, "bash", "echo still-alive", session_id=session_id)
    assert result["return_code"] == 0 and "still-alive" in _plain(result["output"]), (
        f"The session was left unusable after hitting the pids limit: {result}"
    )
    print("The session still executes normally afterwards (as expected)")


async def check_open_files_limit(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """The container runs under the configured RLIMIT_NOFILE, soft and hard alike."""
    result = await _execute(
        session, api_url, "python",
        "import resource\nprint(*resource.getrlimit(resource.RLIMIT_NOFILE))",
        session_id=session_id,
    )
    output = _plain(result["output"]).strip()
    print(f"RLIMIT_NOFILE inside the container: {output!r}")
    assert result["return_code"] == 0, f"Reading RLIMIT_NOFILE failed: {output!r}"
    soft, hard = (int(value) for value in output.split())
    assert soft == hard == cfg.container_ulimit_nofile, (
        f"Expected RLIMIT_NOFILE {cfg.container_ulimit_nofile}:{cfg.container_ulimit_nofile}, got "
        f"{soft}:{hard} -- the server's CONTAINER_ULIMIT_NOFILE may differ from "
        "--container-ulimit-nofile"
    )
    print("RLIMIT_NOFILE matches the configured limit, soft and hard (as expected)")


async def check_output_truncation(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Output past MAX_OUTPUT_SIZE is truncated, and the run still succeeds.

    Truncating rather than failing is the point: a program that prints too much has not done
    anything wrong, and its exit status and its files still matter.
    """
    target_mib = cfg.max_output_size // (1024 * 1024) + 8
    result = await _execute(
        session, api_url, "python",
        "import sys\n"
        f"chunk = 'x' * (1024 * 1024)\nfor _ in range({target_mib}):\n    sys.stdout.write(chunk)\n"
        "sys.stdout.flush()\n"
        "open('finished.txt', 'w').write('the run still completed')",
        session_id=session_id,
    )
    print(f"Output probe: wrote {target_mib}MiB, got back {len(result['output'])} bytes, "
          f"return_code={result['return_code']} timed_out={result['timed_out']}")

    assert result["return_code"] == 0, f"A noisy run should still succeed: {result['output'][-200:]!r}"
    assert not result["timed_out"], "A noisy run should not hit the execution timeout"
    assert len(result["output"]) <= cfg.max_output_size, (
        f"Output was not truncated: got {len(result['output'])} bytes against a "
        f"--max-output-size of {cfg.max_output_size}"
    )
    assert result["files"].get("finished.txt") == b"the run still completed", (
        f"The run's files must survive output truncation: {sorted(result['files'])}"
    )
    print(f"Output truncated to {len(result['output'])} bytes, run and files intact (as expected)")


async def check_outbound_network(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Executed code has outbound network access, which the sandbox grants deliberately.

    The image ships requests, yt-dlp, dnsutils and ping, so this is a feature rather than an
    oversight - and a silent regression to --net=none would break those without failing any
    other check.
    """
    result = await _execute(
        session, api_url, "python",
        "import socket\n"
        "print('dns', socket.gethostbyname('example.com'))\n"
        "s = socket.create_connection(('1.1.1.1', 53), timeout=8)\ns.close()\n"
        "print('tcp ok')",
        session_id=session_id,
    )
    output = _plain(result["output"])
    print(f"Outbound network probe: return_code={result['return_code']} output={output.strip()[:160]!r}")
    assert result["return_code"] == 0 and "tcp ok" in output, (
        "Executed code could not reach the network, which --net=bridge is supposed to allow. "
        f"If this environment simply has no egress, skip this check: {output!r}"
    )
    print("Outbound DNS and TCP both work from inside the sandbox (as expected)")


async def check_ping_without_net_raw(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """`ping` works, and the capability sets are still empty.

    Both halves matter, and only together. `ping` is the one shipped utility --cap-drop=ALL
    appears to break, and its own error message ("missing cap_net_raw+p capability") points at
    the wrong fix: the real cause is podman's default net.ipv4.ping_group_range of `0 0` refusing
    the ICMP datagram socket to a container that runs as CONTAINER_USER_ID, which sends iputils
    down its raw-socket fallback. The --sysctl in podman_executor.py widens the range to that one
    gid and nothing else changes.

    Asserting the capability sets alongside it is what stops the obvious wrong fix from passing
    this check. --cap-add=NET_RAW would also make ping work, but podman adds capabilities as
    *ambient* ones, so it is not /bin/ping that gets CAP_NET_RAW - it is every process the
    executed code spawns, which then has AF_PACKET sockets and can sniff, ARP-poison and spoof
    source addresses on the bridge shared with every concurrently running session.

    The probe pings loopback rather than a public address: the socket call is the gate being
    tested, and ICMP egress is neither required for that nor guaranteed on every network.
    outbound_network is the check that covers reaching the outside world.
    """
    result = await _execute(
        session, api_url, "python",
        "import json, re, socket, subprocess\n"
        "report = {}\n"
        "status = open('/proc/self/status').read()\n"
        "report['caps'] = {n: v for n, v in re.findall(r'^Cap(\\w+):\\s+([0-9a-f]+)$', status, re.M)}\n"
        "report['ping_group_range'] = open('/proc/sys/net/ipv4/ping_group_range').read().split()\n"
        "report['gid'] = socket.os.getgid()\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()\n"
        "    report['icmp_dgram_socket'] = 'ok'\n"
        "except OSError as exc:\n"
        "    report['icmp_dgram_socket'] = type(exc).__name__ + ': ' + str(exc)\n"
        "try:\n"
        "    socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 3).close()\n"
        "    report['af_packet_socket'] = 'OPENED'\n"
        "except OSError as exc:\n"
        "    report['af_packet_socket'] = type(exc).__name__\n"
        "proc = subprocess.run(['ping', '-c', '1', '-W', '2', '127.0.0.1'],\n"
        "                      capture_output=True, text=True, timeout=10)\n"
        "report['ping_rc'] = proc.returncode\n"
        "report['ping_output'] = (proc.stdout + proc.stderr).strip()[-300:]\n"
        "print(json.dumps(report))",
        session_id=session_id,
    )
    output = _plain(result["output"]).strip()
    assert result["return_code"] == 0, f"The ping probe itself failed to run: {output[-300:]!r}"
    report = json.loads(output.split("\n")[-1])
    print(f"ping probe: gid={report['gid']} ping_group_range={report['ping_group_range']} "
          f"icmp_dgram_socket={report['icmp_dgram_socket']!r} af_packet_socket={report['af_packet_socket']!r} "
          f"ping_rc={report['ping_rc']} caps={report['caps']}")

    granted = {name: value for name, value in report["caps"].items() if value.strip("0") != ""}
    assert not granted, (
        f"The container holds capabilities {granted} -- --cap-drop=ALL is no longer in effect. "
        "If this came from adding --cap-add=NET_RAW to make ping work, revert it: podman grants "
        "capabilities ambiently, so every process the executed code spawns would hold CAP_NET_RAW "
        "and with it AF_PACKET sockets, i.e. packet capture, ARP poisoning and source-address "
        "spoofing against the other sessions sharing CONTAINER_NETWORK. The supported fix is the "
        "--sysctl net.ipv4.ping_group_range in podman_executor.py, which needs no capability."
    )
    assert report["af_packet_socket"] != "OPENED", (
        "Executed code opened an AF_PACKET socket, so it has layer-2 access to the bridge it "
        "shares with every concurrently running session"
    )
    assert report["icmp_dgram_socket"] == "ok", (
        "Executed code cannot open an ICMP datagram socket "
        f"({report['icmp_dgram_socket']}), so ping falls back to a raw socket and fails. "
        f"gid {report['gid']} is outside ping_group_range {report['ping_group_range']} -- check "
        "that the --sysctl in podman_executor.py is present and derived from CONTAINER_USER_ID, "
        "and note that podman rejects that flag entirely when CONTAINER_NETWORK=host."
    )
    assert report["ping_rc"] == 0, (
        f"The ICMP datagram socket works but ping still failed: {report['ping_output']!r}"
    )
    print("ping works with every capability still dropped (as expected)")


async def check_api_unreachable_from_sandbox(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """Executed code cannot reach the API that is running it.

    The container gets a routable path back to the host, so whether the API is exposed to the
    code it executes is decided entirely by what it binds to. Bound to a loopback address it is
    unreachable; bound to 0.0.0.0 - which is the default - any executed program can call the
    API, enumerate other sessions and read their files. Nothing else in the sandbox profile
    prevents that, so it is worth asserting explicitly.
    """
    port = URL(api_url).port or 80
    result = await _execute(
        session, api_url, "python",
        "import socket, json\n"
        "results = {}\n"
        "for host in ('host.containers.internal', 'host.docker.internal', '10.88.0.1', '172.17.0.1'):\n"
        "    try:\n"
        f"        s = socket.create_connection((host, {port}), timeout=3)\n"
        "        s.sendall(b'GET /health HTTP/1.0\\r\\n\\r\\n')\n"
        "        results[host] = s.recv(64).decode('latin1')\n"
        "        s.close()\n"
        "    except Exception as exc:\n"
        "        results[host] = type(exc).__name__\n"
        "print(json.dumps(results))",
        session_id=session_id,
    )
    output = _plain(result["output"]).strip()
    print(f"API reachability from the sandbox (port {port}): {output[:300]}")

    reached = [host for host, outcome in json.loads(output.split("\n")[-1]).items() if "HTTP/" in outcome]
    assert not reached, (
        f"Executed code reached the API itself at {reached} -- the server is bound to an address "
        "the container can route to. That is denial of service and resource amplification: "
        "exhaust MAX_SESSIONS so legitimate callers get 503, and call POST /execute for another "
        "container with a fresh budget, so one submission is no longer bounded by the "
        "per-container limits. It is not cross-session disclosure -- nothing lists sessions and "
        "the id never reaches the container. Bind to a loopback address, or point "
        "CONTAINER_NETWORK at a network firewalled away from the API's port."
    )
    print("The API is not reachable from inside the sandbox (as expected)")


async def check_execution_timeout(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A run that overruns EXECUTION_TIMEOUT is cut off, and everything it managed is kept.

    The timeout is the only bound on a program that never finishes, so what it does with the
    work already done matters: output already printed and files already written are real
    results, and throwing them away would make a timeout far less diagnosable than it needs
    to be.
    """
    result = await _execute(
        session, api_url, "python",
        "import sys, time\n"
        "open('written_before_timeout.txt', 'w').write('partial work')\n"
        "print('printed before the timeout', flush=True)\n"
        f"time.sleep({cfg.execution_timeout * 4 + 10})\n"
        "print('this line is never reached')",
        session_id=session_id,
    )
    output = _plain(result["output"])
    print(f"Timeout probe: timed_out={result['timed_out']} return_code={result['return_code']} "
          f"execution_time={result['execution_time']:.1f}s files={sorted(result['files'])}")

    assert result["timed_out"] is True, (
        f"A run sleeping well past --execution-timeout ({cfg.execution_timeout}s) was not marked "
        f"timed out: {result!r} -- the server's EXECUTION_TIMEOUT may differ from --execution-timeout"
    )
    assert result["return_code"] == -1, (
        f"Expected return_code -1 for a timed-out run, got {result['return_code']}"
    )
    assert result["execution_time"] >= cfg.execution_timeout, (
        f"execution_time {result['execution_time']:.1f}s is below the timeout it hit"
    )
    assert "printed before the timeout" in output, (
        f"Output produced before the timeout was discarded: {output!r}"
    )
    assert "never reached" not in output, f"The run continued past its timeout: {output!r}"
    assert result["files"].get("written_before_timeout.txt") == b"partial work", (
        f"Files written before the timeout were not collected: {sorted(result['files'])}"
    )
    print("Timed-out run: partial output and partial files both preserved (as expected)")

    result = await _execute(session, api_url, "bash", "echo still-alive", session_id=session_id)
    assert result["return_code"] == 0 and "still-alive" in _plain(result["output"]), (
        f"The session was left unusable after a timeout: {result!r}"
    )
    print("The session still executes normally after a timeout (as expected)")


async def check_session_capacity(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """At MAX_SESSIONS, creating another is refused with 503 and the API stays healthy.

    Capacity is reported, not enforced by queueing: a caller that gets 503 can retry or free a
    session, which it can only do if the refusal left everything else working.
    """
    async with owned_sessions(session, api_url, cfg.max_sessions) as session_ids:
        print(f"Filled session capacity with {len(session_ids)} sessions")

        async with session.post(f"{api_url}/sessions") as response:
            status, body = response.status, (await response.text())[:160]
        assert status == 503, (
            f"Expected 503 creating a session past --max-sessions ({cfg.max_sessions}), got "
            f"{status}: {body!r} -- the server's MAX_SESSIONS may differ from --max-sessions"
        )
        print(f"POST /sessions at capacity -> 503 {body!r} (as expected)")

        # An ephemeral /execute needs a session of its own, so it is refused the same way
        # rather than running without one.
        data = aiohttp.FormData(default_to_multipart=True)
        data.add_field("language", "bash")
        data.add_field("code", "echo hi")
        async with session.post(f"{api_url}/execute", data=data) as response:
            status, body = response.status, (await response.text())[:160]
        assert status == 503, (
            f"Expected 503 for an ephemeral /execute at capacity, got {status}: {body!r}"
        )
        print(f"POST /execute at capacity -> 503 {body!r} (as expected)")

        # The sessions that do exist are unaffected by the refusals.
        result = await _execute(session, api_url, "bash", "echo still-alive", session_id=session_ids[0])
        assert result["return_code"] == 0, f"An existing session broke while at capacity: {result!r}"
        print("An existing session still executes while the server is at capacity (as expected)")

    async with session.post(f"{api_url}/sessions") as response:
        assert response.status == 200, (
            f"Capacity was not released after deleting the sessions: {response.status}"
        )
        recovered = (await response.json())["session_id"]
    async with session.delete(f"{api_url}/sessions/{recovered}") as response:
        assert response.status == 204
    print("Capacity is available again once sessions are deleted (as expected)")


async def check_ephemeral_sessions_release_slots(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """A throwaway /execute frees its slot, so they cannot accumulate.

    Running more of them back to back than the server could ever hold at once is the whole
    test: if any single call failed to clean up, this runs out of capacity partway through.
    """
    attempts = cfg.max_sessions + 3
    for attempt in range(1, attempts + 1):
        result = await _execute(session, api_url, "bash", f"echo run-{attempt}")
        assert result["return_code"] == 0 and f"run-{attempt}" in _plain(result["output"]), (
            f"Ephemeral execute {attempt} of {attempts} failed: {result!r} -- if this is a 503, "
            "throwaway sessions are not releasing their MAX_SESSIONS slots"
        )
    print(f"{attempts} back-to-back ephemeral executes all succeeded against a "
          f"--max-sessions of {cfg.max_sessions} (as expected)")


async def check_concurrent_execution_limit(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """More concurrent runs than MAX_CONCURRENT_EXECUTIONS queue; they do not all run at once.

    The limit exists to bound how much of the host executing code can occupy at any moment, so
    the observable property is timing: with a limit of N and 2N runs of the same duration, the
    whole batch cannot finish in the time one batch of N takes.
    """
    limit = cfg.max_concurrent_executions
    batch = limit * 2
    sleep_seconds = 3

    async with owned_sessions(session, api_url, batch) as session_ids:
        started = time.monotonic()
        results = await asyncio.gather(*(
            _execute(session, api_url, "bash", f"sleep {sleep_seconds}; echo done", session_id=sid)
            for sid in session_ids
        ))
        elapsed = time.monotonic() - started

    print(f"{batch} concurrent executes against a limit of {limit} took {elapsed:.1f}s "
          f"(one sleep is {sleep_seconds}s)")
    for index, result in enumerate(results):
        assert result["return_code"] == 0, f"Concurrent execute {index} failed: {result!r}"

    assert elapsed >= sleep_seconds * 2 * 0.8, (
        f"{batch} runs against a --max-concurrent-executions of {limit} finished in {elapsed:.1f}s, "
        f"which is too fast for them to have been queued in batches of {limit} -- the semaphore "
        "does not appear to be limiting anything"
    )
    print(f"The batch was serialised into waves rather than run all at once (as expected)")


async def check_session_lock_timeout(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A request for a session that is busy gives up with 409 rather than queueing forever.

    One session runs one thing at a time. A caller that arrives mid-execution has to be told
    so, and told within a bounded time, instead of holding a connection open indefinitely.
    """
    holder = asyncio.create_task(_execute(
        session, api_url, "bash", f"sleep {cfg.lock_wait_timeout * 3 + 5}; echo held",
        session_id=session_id, expected_status=200,
    ))
    try:
        await asyncio.sleep(1.5)  # let the holder take the lock before contending
        started = time.monotonic()
        async with session.get(f"{api_url}/sessions/{session_id}/files/") as response:
            status, body = response.status, (await response.text())[:160]
        waited = time.monotonic() - started
        print(f"GET on a busy session -> {status} after {waited:.1f}s {body!r}")

        assert status == 409, (
            f"Expected 409 for a request against a session busy for longer than "
            f"--lock-wait-timeout ({cfg.lock_wait_timeout}s), got {status}: {body!r} -- the "
            "server's SESSION_LOCK_WAIT_TIMEOUT_SECONDS may differ from --lock-wait-timeout"
        )
        assert waited >= cfg.lock_wait_timeout * 0.5, (
            f"The 409 came back in {waited:.1f}s, far sooner than the {cfg.lock_wait_timeout}s "
            "lock wait -- the request does not appear to have waited for the lock at all"
        )
        print("A busy session refuses a second caller with 409, after waiting (as expected)")
    finally:
        # Cancelling the client request would not stop the run - the server keeps going until
        # its own timeout - so wait for it, or the session is still locked at cleanup time.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(holder, timeout=cfg.execution_timeout + 30)


async def check_idle_session_expiry(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """An idle session is swept; one that is being used is not.

    Expiry is what keeps abandoned sessions from holding capacity forever. It is keyed on last
    use, so the second half matters as much as the first - a sweep that ignored activity would
    delete sessions out from under callers who are still working.
    """
    idle = await _new_session(session, api_url)
    active = await _new_session(session, api_url)
    print(f"Created an idle session ({idle}) and one that will be kept busy ({active})")

    deadline = time.monotonic() + cfg.inactivity_timeout + cfg.sweep_interval * 2 + 5
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(max(1.0, cfg.inactivity_timeout / 4))
            async with session.get(f"{api_url}/sessions/{active}/files/") as response:
                assert response.status == 200, (
                    f"The session being kept busy was swept anyway: {response.status} -- expiry "
                    "must be keyed on last use, and every request updates it"
                )
        print(f"The active session survived {cfg.inactivity_timeout + cfg.sweep_interval * 2 + 5}s "
              "of being touched (as expected)")

        async with session.get(f"{api_url}/sessions/{idle}/files/") as response:
            status = response.status
        assert status == 404, (
            f"Expected the idle session to be swept after --inactivity-timeout "
            f"({cfg.inactivity_timeout}s) plus a sweep interval, got {status} -- the server's "
            "SESSION_INACTIVITY_TIMEOUT_SECONDS may differ from --inactivity-timeout"
        )
        print("The idle session was swept (as expected)")
    finally:
        for session_id in (idle, active):
            with contextlib.suppress(aiohttp.ClientError):
                await session.delete(f"{api_url}/sessions/{session_id}")


async def check_per_file_size_cap(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """A single file larger than CONTAINER_ULIMIT_FSIZE is refused, and nothing is damaged.

    This is the userspace half of the per-file cap - it mirrors the RLIMIT_FSIZE the container
    itself runs under, so a file the API accepts is one executed code could also have written.
    """
    files = f"{api_url}/sessions/{session_id}/files"
    async with session.put(f"{files}/keeper.txt", data=b"must survive") as response:
        assert response.status == 204, f"PUT keeper.txt failed: {response.status}"

    oversized = b"x" * (cfg.max_file_size + 65536)
    async with session.put(f"{files}/toobig.bin", data=oversized) as response:
        status, body = response.status, (await response.text())[:160]
    print(f"PUT {len(oversized)} bytes against a --max-file-size of {cfg.max_file_size} -> {status} {body!r}")
    assert status == 413, (
        f"Expected 413 for a file past --max-file-size ({cfg.max_file_size}), got {status}: {body!r} "
        "-- the server's CONTAINER_ULIMIT_FSIZE may differ from --max-file-size"
    )

    async with session.get(f"{files}/toobig.bin") as response:
        assert response.status == 404, f"The refused upload was left behind: {response.status}"
    print("GET the refused upload -> 404 (as expected, nothing left behind)")

    # And overwriting an existing file with an oversized body must not destroy it.
    async with session.put(f"{files}/keeper.txt", data=oversized) as response:
        assert response.status == 413, f"Expected 413 overwriting with an oversized body, got {response.status}"
    async with session.get(f"{files}/keeper.txt") as response:
        assert response.status == 200, f"The original file vanished: {response.status}"
        content = await response.read()
    assert content == b"must survive", (
        f"A refused overwrite damaged the original file: {content[:64]!r} -- the body is supposed "
        "to stage to a temp file and only be renamed into place once it is whole"
    )
    print("A refused overwrite left the original byte-identical (as expected)")


async def check_quota_survives_project_id_reuse(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """A session that reuses a freed XFS project id is still enforced.

    Project ids are recycled, and the id carries the quota - so a session created right after
    another was deleted gets the id that was just filled to its limit. If the deletion's usage
    did not come back with it, the new session starts life already short of space.
    """
    filler = await _new_session(session, api_url)
    probe_mib = cfg.max_session_size // (1024 * 1024) + 16
    try:
        result = await _execute(
            session, api_url, "bash",
            f"dd if=/dev/zero of=filler.bin bs=1M count={probe_mib} 2>&1", session_id=filler,
        )
        assert result["return_code"] != 0 and "no space" in result["output"].lower(), (
            f"The filler session was not stopped by its quota: {result['output']!r}"
        )
        print(f"Filled a session to its byte quota (wrote until ENOSPC)")
    finally:
        async with session.delete(f"{api_url}/sessions/{filler}") as response:
            assert response.status == 204, f"DELETE of the filled session failed: {response.status}"

    # The next session is the one that gets the recycled id.
    async with owned_sessions(session, api_url, 1) as (successor,):
        result = await _execute(
            session, api_url, "bash", "dd if=/dev/zero of=small.bin bs=1M count=8 2>&1",
            session_id=successor,
        )
        print(f"8MiB write into the successor session: return_code={result['return_code']} "
              f"output={result['output']!r}")
        assert result["return_code"] == 0, (
            f"A small write into a session that reused a just-freed project id failed: "
            f"{result['output']!r} -- the deleted session's usage was not released with its id, so "
            "the new session inherited a quota that is already spent"
        )
        print("A session reusing a freed project id has its full quota (as expected)")


def _podman_managed_containers() -> list[str]:
    output = subprocess.run(
        ["podman", "ps", "-a", "--filter", "label=code_executor_api.managed=true",
         "--format", "{{.Names}} {{.Status}}"],
        capture_output=True, text=True, timeout=30,
    )
    return [line for line in output.stdout.splitlines() if line.strip()]


async def check_no_leaked_containers(session: aiohttp.ClientSession, api_url: str, cfg: Config, session_id: str) -> None:
    """No container outlives the request that started it, including one that timed out.

    `podman run --rm` handles the ordinary exits; a run that is still going when its timeout
    fires has to be removed explicitly, and that removal happens in a cleanup path that
    nothing else observes.
    """
    assert shutil.which("podman"), "podman is not on PATH"

    before = _podman_managed_containers()
    print(f"Managed containers before: {before or 'none'}")

    result = await _execute(session, api_url, "bash", "echo quick", session_id=session_id)
    assert result["return_code"] == 0, f"The probe run failed: {result!r}"

    # Give an ordinary --rm teardown a moment to complete before looking.
    await asyncio.sleep(2)
    after = _podman_managed_containers()
    leaked = [line for line in after if line not in before]
    print(f"Managed containers after: {after or 'none'}")
    assert not leaked, (
        f"Containers outlived their request: {leaked} -- these carry the "
        "code_executor_api.managed label, so they were started by this API and never removed"
    )
    print("No containers left behind (as expected)")


async def check_session_directory_removed(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """Deleting a session removes its directory from disk, whatever executed code left there.

    Executed code owns the files it creates, including their permissions, so it can leave the
    session in a state the API has to cope with when cleaning up. The API reports 204 either
    way - only the filesystem shows whether the deletion actually happened.
    """
    assert cfg.session_root, "--session-root is required for this check"
    root = pathlib.Path(cfg.session_root)
    assert root.is_dir(), f"--session-root {cfg.session_root!r} is not a directory"

    before = {path.name for path in root.glob("code_executor_session_*")}
    session_id = await _new_session(session, api_url)

    # A directory the API cannot traverse. The owner bits apply to the API too, so this is
    # not a privilege trick - it is what any ordinary program can do to its own files.
    result = await _execute(
        session, api_url, "bash",
        "mkdir -p stubborn && echo content > stubborn/file.txt && chmod 000 stubborn && echo prepared",
        session_id=session_id,
    )
    assert result["return_code"] == 0, f"Preparing the unreadable directory failed: {result['output']!r}"

    created = {path.name for path in root.glob("code_executor_session_*")} - before
    assert len(created) == 1, (
        f"Expected exactly one new session directory under {cfg.session_root!r}, found {created} "
        "-- is --session-root pointing at the server's SESSION_ROOT_DIRECTORY?"
    )
    directory = root / created.pop()
    print(f"Session directory on disk: {directory}")

    async with session.delete(f"{api_url}/sessions/{session_id}") as response:
        assert response.status == 204, f"DELETE session failed: {response.status}"

    await asyncio.sleep(1)
    assert not directory.exists(), (
        f"DELETE reported success but {directory} is still on disk. Executed code left a "
        "directory the API cannot traverse, and the recursive removal ignores errors, so the "
        "session's files - and the session directory itself - survive its deletion. Nothing "
        "ever removes them: the inactivity sweep takes the same path and fails the same way."
    )
    print("The session directory is gone from disk after DELETE (as expected)")


async def check_no_orphan_session_directories(session: aiohttp.ClientSession, api_url: str, cfg: Config) -> None:
    """Throwaway sessions leave nothing behind on disk."""
    assert cfg.session_root, "--session-root is required for this check"
    root = pathlib.Path(cfg.session_root)
    assert root.is_dir(), f"--session-root {cfg.session_root!r} is not a directory"

    before = {path.name for path in root.glob("code_executor_session_*")}
    for attempt in range(3):
        result = await _execute(session, api_url, "bash", f"echo ephemeral-{attempt} > leftover.txt")
        assert result["return_code"] == 0, f"Ephemeral execute failed: {result!r}"

    await asyncio.sleep(1)
    after = {path.name for path in root.glob("code_executor_session_*")}
    orphans = after - before
    print(f"Session directories before: {len(before)}, after: {len(after)}")
    assert not orphans, (
        f"Throwaway /execute sessions left directories behind: {sorted(orphans)}"
    )
    print("Throwaway sessions left no directories on disk (as expected)")


type CheckFn = Callable[..., Awaitable[None]]


@dataclasses.dataclass(frozen=True, slots=True)
class Group:
    name: str
    opt_in: bool = False
    requires: str = ""  # the server configuration this group needs, printed when gated or run


@dataclasses.dataclass(frozen=True, slots=True)
class Check:
    name: str  # the function name minus its "check_" prefix; what --only/--skip take
    fn: CheckFn
    group: str = "core"
    sessions: int = 1  # fresh sessions the runner creates and destroys around the check
    heavy: bool = False  # stresses the host or moves a lot of data; --skip-heavy drops it
    timeout: float = 120.0
    summary: str = ""


GROUPS: tuple[Group, ...] = (
    Group("core"),
    Group("languages"),
    Group("sandbox"),
    Group("results"),
    Group("errors"),
    Group("network"),
    Group("timeout", opt_in=True, requires="EXECUTION_TIMEOUT small (5s or less), passed as --execution-timeout"),
    Group("capacity", opt_in=True,
          requires="MAX_SESSIONS and MAX_CONCURRENT_EXECUTIONS both small (4 or less), passed as "
                   "--max-sessions/--max-concurrent-executions; run this group on its own, since it "
                   "saturates limits the other checks need"),
    Group("lock", opt_in=True,
          requires="SESSION_LOCK_WAIT_TIMEOUT_SECONDS small (2s or less), passed as --lock-wait-timeout"),
    Group("sweep", opt_in=True,
          requires="SESSION_INACTIVITY_TIMEOUT_SECONDS and SESSION_SWEEP_INTERVAL_SECONDS both small "
                   "(5s/2s), passed as --inactivity-timeout/--sweep-interval"),
    Group("fsize", opt_in=True,
          requires="CONTAINER_ULIMIT_FSIZE well below MAX_SESSION_SIZE (say 1 MiB), passed as "
                   "--max-file-size; C# needs the production 256 MiB, so skip the languages group there"),
    Group("host", opt_in=True,
          requires="running on the API host, with podman on PATH and --session-root set to the "
                   "server's SESSION_ROOT_DIRECTORY"),
    Group(
        "quota", opt_in=True,
        requires="SESSION_QUOTA_MOUNTPOINT set to a working XFS prjquota mount point; pass "
                 "--max-session-size/--max-session-entries if they differ from the defaults",
    ),
)

# Declaration order is run order.
CHECKS: tuple[Check, ...] = (
    Check("health", check_health, sessions=0,
          summary="GET /health returns ok"),
    Check("seeded_session", check_seeded_session, sessions=0,
          summary="POST /sessions with multipart seed files"),
    Check("file_lifecycle", check_file_lifecycle,
          summary="PUT/GET/DELETE a session file"),
    Check("session_deletion", check_session_deletion, sessions=0,
          summary="DELETE a session makes its files unreachable"),
    Check("execute_persistence", check_execute_persistence,
          summary="files persist and deletions are detected across two runs"),
    Check("execute_attachments", check_execute_attachments,
          summary="/execute attachments are staged into the session"),
    Check("symlink_attachment_excluded", check_symlink_attachment_excluded, group="sandbox",
          summary="a symlink is neither attached nor followed"),
    Check("symlink_directory_escape_blocked", check_symlink_directory_escape_blocked, group="sandbox",
          summary="a symlinked directory cannot be used to escape the session root"),
    Check("readonly_root_filesystem", check_readonly_root_filesystem, group="sandbox",
          summary="the container root filesystem is read-only"),
    Check("open_files_limit", check_open_files_limit, group="sandbox",
          summary="RLIMIT_NOFILE matches CONTAINER_ULIMIT_NOFILE"),
    Check("memory_limit", check_memory_limit, group="sandbox", heavy=True, timeout=180.0,
          summary="allocating past MAX_MEMORY is killed, not hung"),
    Check("pids_limit", check_pids_limit, group="sandbox", heavy=True, timeout=180.0,
          summary="process creation is capped and the session survives it"),
    Check("output_truncation", check_output_truncation, group="sandbox", heavy=True, timeout=180.0,
          summary="output past MAX_OUTPUT_SIZE truncates without failing the run"),
    Check("outbound_network", check_outbound_network, group="network",
          summary="the sandbox has outbound DNS and TCP, as --net=bridge intends"),
    Check("api_unreachable_from_sandbox", check_api_unreachable_from_sandbox, group="network",
          summary="executed code cannot reach the API that is running it"),
    Check("ping_without_net_raw", check_ping_without_net_raw, group="network",
          summary="ping works with every capability still dropped"),
    Check("directory_listing", check_directory_listing,
          summary="GET on a directory lists one level and hides nothing"),
    Check("disk_quota_enforcement", check_disk_quota_enforcement, group="quota", heavy=True, timeout=300.0,
          summary="MAX_SESSION_SIZE stops both the container and a PUT"),
    Check("inode_quota_enforcement", check_inode_quota_enforcement, group="quota", heavy=True, timeout=300.0,
          summary="MAX_SESSION_ENTRIES stops runaway file creation"),
    Check("rejected_execute_leaves_no_attachment", check_rejected_execute_leaves_no_attachment, group="errors",
          summary="a rejected /execute leaves its attachments behind nowhere"),
    Check("javascript_module_styles", check_javascript_module_styles, group="languages",
          summary="CommonJS, ESM and top-level await all run"),
    Check("typescript", check_typescript, group="languages",
          summary="annotations, require, enum and relative imports all run"),
    Check("typescript_rejects_top_level_await", check_typescript_rejects_top_level_await, group="languages",
          summary="typescript is CommonJS, so top-level await must fail"),
    Check("compiled_languages", check_compiled_languages, group="languages", timeout=300.0,
          summary="c/cpp/java/csharp/rust each run a hello world and leave no session files"),
    Check("compiled_language_errors", check_compiled_language_errors, group="languages", timeout=300.0,
          summary="a compile error fails the run and surfaces diagnostics"),
    Check("change_detection_stability", check_change_detection_stability, group="results",
          summary="no-op runs report nothing for either file origin"),
    Check("home_directory_noise_excluded", check_home_directory_noise_excluded, group="results",
          summary="hidden $HOME directories stay out of attachments but stay listable"),
    Check("result_attachment_cap", check_result_attachment_cap, group="results", timeout=300.0,
          summary="MAX_RESULT_ATTACHMENTS caps the response without failing the run"),
    Check("deleted_files_not_truncated", check_deleted_files_not_truncated, group="results", timeout=300.0,
          summary="deleted_files is reported in full, past the attachment cap"),
    Check("awkward_result_filenames", check_awkward_result_filenames, group="results",
          summary="quotes, spaces, UTF-8 and nested paths survive Content-Disposition exactly"),
    Check("non_utf8_filename_omitted", check_non_utf8_filename_omitted, group="results",
          summary="a non-UTF-8 filename is omitted, not a 500, and stays listable"),
    Check("change_detection_adversarial", check_change_detection_adversarial, group="results",
          summary="restored mtime, delete-and-recreate and rename are all detected"),
    Check("hidden_file_vs_hidden_directory", check_hidden_file_vs_hidden_directory, group="results",
          summary="a hidden file is a result; a hidden directory's contents are not"),
    Check("ephemeral_execute", check_ephemeral_execute, sessions=0,
          summary="POST /execute without a session id"),
    Check("put_path_edges", check_put_path_edges,
          summary="nested parents, onto a directory, through a file, and a zero-byte body"),
    Check("put_replaces_symlink", check_put_replaces_symlink,
          summary="PUT onto a symlink replaces the link instead of following it"),
    Check("delete_path_edges", check_delete_path_edges,
          summary="DELETE refuses directories and the root, removes symlinks"),
    Check("special_file_types", check_special_file_types,
          summary="a FIFO and a socket list as \"other\" and read back 404"),
    Check("sub_path_containment", check_sub_path_containment,
          summary="odd sub_paths land inside the session or are refused, never outside"),
    Check("root_listing_forms", check_root_listing_forms,
          summary="/files/ lists, /files 404s, a missing nested path 404s"),
    Check("interrupted_put_leaves_original", check_interrupted_put_leaves_original,
          summary="an aborted PUT preserves the original and leaves no staging file"),
    Check("error_cases", check_error_cases, group="errors",
          summary="unknown session, bad language and path traversal"),
    Check("execute_field_validation", check_execute_field_validation, group="errors",
          summary="unknown fields, missing fields, NUL code and escaping attachments all 400"),
    Check("execute_oversized_code", check_execute_oversized_code, group="errors",
          summary="code past MAX_CODE_LENGTH -> 413, staged attachment discarded"),
    Check("execute_malformed_body", check_execute_malformed_body, group="errors",
          summary="a JSON body or a boundary-less multipart body -> 400, not 500"),
    Check("execute_oversized_language", check_execute_oversized_language, group="errors",
          summary="an over-long language field is a bad language (400), not a size error"),
    Check("nul_byte_in_path", check_nul_byte_in_path, group="errors",
          summary="a NUL byte in a sub_path is an invalid path (400), not a 500"),
    Check("session_seed_validation", check_session_seed_validation, group="errors", sessions=0,
          summary="a rejected seeded create returns 400 and leaks no session slot"),
    Check("execution_timeout", check_execution_timeout, group="timeout", timeout=300.0,
          summary="an overrunning run is cut off, keeping its partial output and files"),
    Check("session_capacity", check_session_capacity, group="capacity", sessions=0, timeout=300.0,
          summary="at MAX_SESSIONS both /sessions and ephemeral /execute return 503"),
    Check("ephemeral_sessions_release_slots", check_ephemeral_sessions_release_slots,
          group="capacity", sessions=0, timeout=300.0,
          summary="back-to-back throwaway runs never exhaust capacity"),
    Check("concurrent_execution_limit", check_concurrent_execution_limit,
          group="capacity", sessions=0, timeout=300.0,
          summary="more runs than MAX_CONCURRENT_EXECUTIONS are queued, not run at once"),
    Check("session_lock_timeout", check_session_lock_timeout, group="lock", timeout=300.0,
          summary="a request against a busy session gives up with 409"),
    Check("idle_session_expiry", check_idle_session_expiry, group="sweep", sessions=0, timeout=300.0,
          summary="an idle session is swept; one in use is not"),
    Check("per_file_size_cap", check_per_file_size_cap, group="fsize", heavy=True, timeout=300.0,
          summary="a file past CONTAINER_ULIMIT_FSIZE is refused without damaging the original"),
    Check("quota_survives_project_id_reuse", check_quota_survives_project_id_reuse,
          group="quota", sessions=0, heavy=True, timeout=300.0,
          summary="a session reusing a freed XFS project id still gets its full quota"),
    Check("no_leaked_containers", check_no_leaked_containers, group="host",
          summary="no container outlives the request that started it"),
    Check("session_directory_removed", check_session_directory_removed, group="host", sessions=0,
          summary="DELETE really removes the session directory, whatever code left in it"),
    Check("no_orphan_session_directories", check_no_orphan_session_directories, group="host", sessions=0,
          summary="throwaway sessions leave no directories on disk"),
)

_GROUP_NAMES = frozenset(group.name for group in GROUPS)
_CHECK_NAMES = frozenset(check.name for check in CHECKS)

# The registry is the one place a typo would silently drop coverage, so it checks itself.
assert len(_CHECK_NAMES) == len(CHECKS), "duplicate check name in CHECKS"
assert len(_GROUP_NAMES) == len(GROUPS), "duplicate group name in GROUPS"
assert {check.group for check in CHECKS} <= _GROUP_NAMES, (
    f"unknown group: {sorted({check.group for check in CHECKS} - _GROUP_NAMES)}"
)
assert not (_CHECK_NAMES & _GROUP_NAMES), (
    f"a check and a group share a name, which would make --only ambiguous: "
    f"{sorted(_CHECK_NAMES & _GROUP_NAMES)}"
)


@dataclasses.dataclass(frozen=True, slots=True)
class Result:
    name: str
    status: str  # "PASS" | "FAIL" | "ERROR" | "SKIP"
    seconds: float
    detail: str = ""
    traceback_text: str = ""


def _assertion_origin(exc: BaseException) -> str:
    """The source line an assertion failed on.

    The message already carries the observed value, so the full traceback is noise; the
    line is what says which of a check's dozen assertions gave way.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    frame = frames[-1]
    return f"harness.py:{frame.lineno}  {(frame.line or '').strip()}"


async def _run_one(
        session: aiohttp.ClientSession, api_url: str, cfg: Config, check: Check,
        *, full_traceback: bool,
) -> Result:
    print(f"\n===== {check.name} [{check.group}] =====")
    started = time.monotonic()
    try:
        # The timeout sits inside owned_sessions so a hung check still has its sessions
        # deleted, rather than wedging the rest of the run behind a held lock.
        async with owned_sessions(session, api_url, check.sessions) as session_ids:
            async with asyncio.timeout(check.timeout):
                await check.fn(session, api_url, cfg, *session_ids)
    except AssertionError as exc:
        detail = str(exc) or "assertion failed"
        return Result(check.name, "FAIL", time.monotonic() - started, detail,
                      traceback.format_exc() if full_traceback else _assertion_origin(exc))
    except TimeoutError:
        return Result(check.name, "ERROR", time.monotonic() - started,
                      f"exceeded the check's {check.timeout:g}s budget")
    except Exception as exc:  # a harness has to survive any check; KeyboardInterrupt still aborts
        return Result(check.name, "ERROR", time.monotonic() - started,
                      f"{type(exc).__name__}: {exc}", traceback.format_exc())
    return Result(check.name, "PASS", time.monotonic() - started)


def select_checks(
        checks: Sequence[Check], *, only: Sequence[str], skip: Sequence[str],
        enabled_groups: AbstractSet[str], skip_heavy: bool,
) -> tuple[list[Check], list[tuple[Check, str]]]:
    """Return (to_run, [(check, skip_reason)]), both in registry order."""
    gated = {group.name: group for group in GROUPS if group.opt_in}
    # Naming an opt-in check or group in --only enables it: having to also pass its
    # --check-<group> flag to run exactly one check would be pure ceremony.
    implied = set(enabled_groups)
    for name in only:
        if name in gated:
            implied.add(name)
        for check in checks:
            if check.name == name:
                implied.add(check.group)

    to_run: list[Check] = []
    skipped: list[tuple[Check, str]] = []
    for check in checks:
        identifiers = {check.name, check.group}
        if only and not (identifiers & set(only)):
            continue
        if identifiers & set(skip):
            skipped.append((check, "explicitly skipped"))
        elif check.group in gated and check.group not in implied:
            group = gated[check.group]
            skipped.append((check, f"opt-in: pass --check-{group.name} (requires {group.requires})"))
        elif skip_heavy and check.heavy:
            skipped.append((check, "heavy, and --skip-heavy was passed"))
        else:
            to_run.append(check)
    return to_run, skipped


def _unknown_names(names: Sequence[str]) -> list[str]:
    return sorted(set(names) - _CHECK_NAMES - _GROUP_NAMES)


def _indent(text: str) -> str:
    return "".join(f"    {line}\n" for line in text.rstrip().splitlines())


async def run(
        api_url: str, cfg: Config, to_run: Sequence[Check], skipped: Sequence[tuple[Check, str]],
        *, fail_fast: bool = False, full_traceback: bool = False,
) -> int:
    results: list[Result] = []
    started = time.monotonic()
    announced_groups: set[str] = set()
    group_requires = {group.name: group.requires for group in GROUPS if group.opt_in}

    async with aiohttp.ClientSession() as session:
        for check in to_run:
            if check.group in group_requires and check.group not in announced_groups:
                announced_groups.add(check.group)
                print(f"\n-- group {check.group} requires: {group_requires[check.group]}")
            result = await _run_one(session, api_url, cfg, check, full_traceback=full_traceback)
            results.append(result)
            print(f"{result.status} {result.name} ({result.seconds:.2f}s)"
                  + (f": {result.detail}" if result.detail else ""))
            if result.traceback_text:
                print(_indent(result.traceback_text))
            if fail_fast and result.status in ("FAIL", "ERROR"):
                print("\nStopping at the first failure (--fail-fast).")
                break

    for check, reason in skipped:
        results.append(Result(check.name, "SKIP", 0.0, reason))

    counts = {status: sum(1 for r in results if r.status == status) for status in ("PASS", "FAIL", "ERROR", "SKIP")}
    print("\n===== summary =====")
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['ERROR']} errored, "
          f"{counts['SKIP']} skipped in {time.monotonic() - started:.1f}s")

    failures = [r for r in results if r.status in ("FAIL", "ERROR")]
    for result in failures:
        print(f"  {result.status} {result.name}: {result.detail}")
    for result in (r for r in results if r.status == "SKIP"):
        print(f"  SKIP {result.name}: {result.detail}")

    if failures:
        print(f"\nRe-run just these: python harness.py --only {' '.join(r.name for r in failures)}")
        return 1
    print("All harness checks passed.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local harness for the CodeExecutorAPI server.",
        epilog="--only and --skip take either a check name or a group name (see --list). "
               "--only implies the opt-in flag for whatever it selects.",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:40003", help="Base URL for the API server")
    parser.add_argument("--list", action="store_true", help="List the groups and checks, then exit")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME",
                        help="Run only these checks or groups")
    parser.add_argument("--skip", nargs="+", default=[], metavar="NAME",
                        help="Skip these checks or groups")
    parser.add_argument("--skip-heavy", action="store_true",
                        help="Skip checks that stress the host or move a lot of data")
    parser.add_argument("-x", "--fail-fast", action="store_true",
                        help="Stop at the first failing check instead of running the rest")
    parser.add_argument("--full-traceback", action="store_true",
                        help="Print a full traceback for assertion failures, not just the failing line")

    for group in GROUPS:
        if group.opt_in:
            parser.add_argument(
                f"--check-{group.name}", action="store_true",
                help=f"Also run the {group.name} checks (requires {group.requires})",
            )
    parser.add_argument("--check-all", action="store_true",
                        help="Enable every opt-in group at once. No single server configuration "
                             "satisfies all of them, so this is for a purpose-built server")
    _add_config_arguments(parser)
    return parser


def _print_listing() -> None:
    print("Groups:")
    for group in GROUPS:
        gate = f"  (opt-in: --check-{group.name})" if group.opt_in else ""
        print(f"  {group.name:<10}{gate}")
        if group.requires:
            print(f"             requires {group.requires}")
    print("\nChecks:")
    for check in CHECKS:
        flags = " [heavy]" if check.heavy else ""
        print(f"  {check.name:<38} {check.group:<10}{flags} {check.summary}")


def main() -> int:
    if not __debug__:
        raise SystemExit("harness.py must not run under -O: every check is an assert, so all "
                         "of them would be stripped and the run would report success having "
                         "verified nothing.")

    parser = _build_parser()
    args = parser.parse_args()

    if args.list:
        _print_listing()
        return 0

    unknown = _unknown_names([*args.only, *args.skip])
    if unknown:
        parser.error(f"unknown check or group name(s): {', '.join(unknown)} (see --list)")

    enabled_groups = {
        group.name for group in GROUPS
        if group.opt_in and (args.check_all or getattr(args, f"check_{group.name}"))
    }
    if args.check_all:
        print("--check-all: no single server configuration satisfies every opt-in group at "
              "once; expect failures unless this server was built for the combination.")

    to_run, skipped = select_checks(
        CHECKS, only=args.only, skip=args.skip,
        enabled_groups=enabled_groups, skip_heavy=args.skip_heavy,
    )
    if not to_run:
        print("No checks selected.")
        for check, reason in skipped:
            print(f"  SKIP {check.name}: {reason}")
        return 0

    return asyncio.run(run(
        args.api_url, _config_from_args(args), to_run, skipped,
        fail_fast=args.fail_fast, full_traceback=args.full_traceback,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
