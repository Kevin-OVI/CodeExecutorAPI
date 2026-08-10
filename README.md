# Code Executor API (aiohttp + Docker)

Code Executor API runs untrusted code inside hardened, ephemeral Docker containers (no capabilities, read-only root, resource limits) and exposes the result over HTTP. Callers manage a persistent **session** (a server-side working directory) so files can be created, read, and deleted across multiple executions without re-uploading a whole directory snapshot each time.

## Features

- `aiohttp` API with session management (`/sessions`), per-file access (`/sessions/{id}/files/{path}`), code execution (`/execute`), and `/health`
- Sandboxed execution via `docker run` with CPU/memory/pid/ulimit caps and a hard wall-clock timeout
- Supports python, bash, javascript, c, java
- Sessions persist a working directory across executions, guarded by a per-session lock; idle sessions expire automatically
- `execute` reports exactly what changed: created/modified files (returned as multipart attachments) and deleted files

## Requirements

- Python 3.12+
- Docker
- A Docker image must be created before starting the API using `Dockerfile`
  ```cmd
  docker build -t code_executor executor_image
  ```

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

The service reads these environment variables at import/startup (see `code_executor_api/config.py`):

- `HOST` (default: `0.0.0.0`)
- `PORT` (default: `40003`)
- `EXECUTION_TIMEOUT` (default: `20` seconds)
- `MAX_MEMORY` (default: `256M`)
- `MAX_CPU_CORES` (default: `1`)
- `MAX_OUTPUT_SIZE` (default: `10485760` bytes)
- `MAX_REQUEST_SIZE` (default: `16777216` bytes)
- `MAX_SESSION_SIZE` (default: `104857600` bytes)
- `MAX_SESSION_FILES` (default: `1000`)
- `MAX_SESSIONS` (default: `64`)
- `MAX_CONCURRENT_EXECUTIONS` (default: `4`)
- `CONTAINER_PIDS_LIMIT` (default: `128`)
- `CONTAINER_ULIMIT_NOFILE` (default: `1024`)
- `CONTAINER_ULIMIT_FSIZE` (default: `10485760` bytes)
- `CONTAINER_RELATIVE_NICENESS` (default: `5`)
- `CONTAINER_TMPFS_SIZE` (default: `64m`)
- `DOCKER_IMAGE` (default: `code_executor`)
- `DOCKER_CHECK_TIMEOUT_SECONDS` (default: `5`)
- `SESSION_INACTIVITY_TIMEOUT_SECONDS` (default: `1800`) - idle sessions are deleted after this long
- `SESSION_SWEEP_INTERVAL_SECONDS` (default: `60`) - how often the expiry sweep runs
- `SESSION_LOCK_WAIT_TIMEOUT_SECONDS` (default: `30`) - how long a request waits for a session's lock before returning `409`
- `SESSION_ROOT_DIRECTORY` (default: the system temporary directory) - use a quota-backed filesystem for a hard storage limit during execution

Running a second (e.g. test) deployment means pointing a separate process at a separate `PORT`/`DOCKER_IMAGE` via its own environment.

## Run API

Default host/port (from env or defaults):

```cmd
python app.py
```

Override host/port from CLI:

```cmd
python app.py --host 127.0.0.1 --port 40003
```

## API

### Health check

```cmd
curl http://127.0.0.1:40003/health
```

### Sessions

Create a session (optionally seeding files via multipart, filename = relative sub_path):

```cmd
curl -X POST http://127.0.0.1:40003/sessions
```

Response: `{"session_id": "..."}`

Delete a session immediately:

```cmd
curl -X DELETE http://127.0.0.1:40003/sessions/{session_id}
```

### Session files

```cmd
curl http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
curl -X PUT --data-binary @localfile.txt http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
curl -X DELETE http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
```

- `GET`/`PUT`/`DELETE` on a file return `404` if the session or file doesn't exist.
- `PUT` creates or overwrites the file (parent directories are created as needed); the request body is the raw file bytes.

### Execute code

`POST /execute` as `multipart/form-data`:

- `session_id` (optional text field) - if given, must reference a live session (`404` otherwise); if omitted, a throwaway session is created and destroyed for this call only
- `language` (text field) - one of `python`, `bash`, `javascript`, `c`, `java`
- `code` (text field)
- `attachments` (optional file parts, filename = sub_path) - created/overwritten in the session before execution

Response is `multipart/mixed`: the first part is `application/json` -

```json
{"output": "...", "return_code": 0, "execution_time": 0.42, "timed_out": false, "deleted_files": []}
```

- followed by one file part per file created or modified during the run (`Content-Disposition: attachment; filename="<sub_path>"`).

Error statuses: `400` invalid input (bad language, invalid path), `404` missing session, `409` session lock timeout, `413` request/session/result limit, `503` unavailable capacity, `500` unexpected error fallback.

## Local Harness

```cmd
python harness.py
python harness.py --api-url http://127.0.0.1:40003
```

Creates a session, PUTs/GETs a file, runs two `/execute` calls sharing the same `session_id` to prove file persistence and deletion detection across executions, then deletes the session and confirms it's gone.

## Project Layout

- `app.py` - CLI entrypoint and server startup
- `harness.py` - local smoke-test script
- `code_executor_api/app_factory.py` - app wiring, startup, and cleanup hooks
- `code_executor_api/config.py` - environment-backed constants
- `code_executor_api/validation.py` - sub_path normalization, language/null-byte validation
- `code_executor_api/sessions.py` - `Session`/`SessionManager`: locking, creation/deletion, expiry sweep
- `code_executor_api/executor/docker_executor.py` - Docker container invocation and file-diffing
- `code_executor_api/routes/` - `/sessions`, `/sessions/{id}/files/{path}`, `/execute`, `/health` handlers

## Dependencies

From `requirements.txt`:

- `aiohttp`
- `aiofiles`
