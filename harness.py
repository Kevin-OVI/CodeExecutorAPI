import argparse
import asyncio
import json

import aiohttp


async def _execute(session: aiohttp.ClientSession, api_url: str, session_id: str, language: str, code: str) -> dict:
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("session_id", session_id)
    form.add_field("language", language)
    form.add_field("code", code)

    async with session.post(f"{api_url}/execute", data=form) as response:
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(f"/execute failed with status {response.status}: {body}")

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


async def run(api_url: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{api_url}/sessions") as response:
            data = await response.json()
            session_id = data["session_id"]
        print(f"Created session: {session_id}")

        async with session.put(f"{api_url}/sessions/{session_id}/files/hello.txt", data=b"hello world") as response:
            assert response.status == 204, f"PUT failed: {response.status}"
        print("PUT hello.txt -> 204")

        async with session.get(f"{api_url}/sessions/{session_id}/files/hello.txt") as response:
            assert response.status == 200, f"GET failed: {response.status}"
            content = await response.read()
            assert content == b"hello world", f"Unexpected content: {content!r}"
        print(f"GET hello.txt -> {content!r}")

        result = await _execute(
            session, api_url, session_id, "python",
            "with open('hello.txt') as f:\n    data = f.read()\nwith open('output.txt', 'w') as f:\n    f.write(data.upper())\nprint('done')",
        )
        print(f"Execute #1: output={result['output']!r} return_code={result['return_code']} "
              f"files={list(result['files'])} deleted_files={result['deleted_files']}")
        assert result["files"].get("output.txt") == b"HELLO WORLD", "output.txt was not persisted correctly"

        result = await _execute(session, api_url, session_id, "python", "import os\nos.remove('output.txt')\nprint('removed')")
        print(f"Execute #2: output={result['output']!r} deleted_files={result['deleted_files']}")
        assert "output.txt" in result["deleted_files"], "output.txt deletion was not detected"

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
    args = parser.parse_args()
    asyncio.run(run(args.api_url))
