# Code Executor API (aiohttp + Docker)

Code Executor API runs untrusted code inside hardened, ephemeral Docker containers (no capabilities, read-only root, resource limits) and exposes the result over HTTP. Callers manage a persistent **session** (a server-side working directory) so files can be created, read, and deleted across multiple executions without re-uploading a whole directory snapshot each time.

## Features

- `aiohttp` API with session management (`/sessions`), per-file access (`/sessions/{id}/files/{path}`), code execution (`/execute` and `/sessions/{id}/execute`), and `/health`
- Sandboxed execution via `docker run` with CPU/memory/pid/ulimit caps and a hard wall-clock timeout
- Supports python, bash, javascript, c, c++, java, c#, rust
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
- `MAX_CODE_LENGTH` (default: `65536` bytes) - must stay below the kernel's `MAX_ARG_STRLEN` (128 KiB) since code is passed as a single `docker run` argv entry
- `MAX_SESSION_SIZE` (default: `104857600` bytes)
- `MAX_SESSIONS` (default: `64`)
- `MAX_CONCURRENT_EXECUTIONS` (default: `4`)
- `CONTAINER_PIDS_LIMIT` (default: `128`)
- `CONTAINER_ULIMIT_NOFILE` (default: `1024`)
- `CONTAINER_ULIMIT_FSIZE` (default: `268435456` bytes)
- `CONTAINER_RELATIVE_NICENESS` (default: `5`)
- `CONTAINER_TMPFS_SIZE` (default: `64m`)
- `DOCKER_IMAGE` (default: `code_executor`)
- `DOCKER_CHECK_TIMEOUT_SECONDS` (default: `5`)
- `SESSION_INACTIVITY_TIMEOUT_SECONDS` (default: `1800`) - idle sessions are deleted after this long
- `SESSION_SWEEP_INTERVAL_SECONDS` (default: `60`) - how often the expiry sweep runs
- `SESSION_LOCK_WAIT_TIMEOUT_SECONDS` (default: `30`) - how long a request waits for a session's lock (or an execution slot) before returning `409`/`503`
- `SESSION_ROOT_DIRECTORY` (default: the system temporary directory) - base directory for session working directories
- `SESSION_QUOTA_MOUNTPOINT` (default: unset) - XFS mountpoint containing `SESSION_ROOT_DIRECTORY`; when set, `MAX_SESSION_SIZE` is enforced as a hard, kernel-level XFS project quota per session (see below). When unset, `MAX_SESSION_SIZE` is only enforced against host-mediated writes (`PUT`/seed/attachment uploads) - code running inside the container can otherwise write past it, bounded only by `CONTAINER_ULIMIT_FSIZE` per file and `EXECUTION_TIMEOUT`

Running a second (e.g. test) deployment means pointing a separate process at a separate `PORT`/`DOCKER_IMAGE` via its own environment.

### Enforcing `MAX_SESSION_SIZE` with an XFS project quota

Without `SESSION_QUOTA_MOUNTPOINT`, `MAX_SESSION_SIZE` only bounds files written through the API itself; it does not cap what executed code writes directly into the session's mounted working directory. To get a real, kernel-enforced cap that also covers code execution, put `SESSION_ROOT_DIRECTORY` on an XFS filesystem with project quotas enabled:

```bash
apt install xfsprogs
mkfs.xfs /dev/vdb                              # a dedicated disk/partition
mkdir -p /var/lib/code_executor/sessions
```

`/etc/fstab`:

```
/dev/vdb  /var/lib/code_executor  xfs  defaults,pquota  0  2
```

```bash
mount -a
xfs_quota -x -c 'state' /var/lib/code_executor   # confirm "Project quota state: ON"
```

Then configure the service:

```
SESSION_ROOT_DIRECTORY=/var/lib/code_executor/sessions
SESSION_QUOTA_MOUNTPOINT=/var/lib/code_executor
```

On session creation, `SessionManager` allocates a project id and runs `xfs_quota -x -c 'project -s -p <dir> <id>' -c 'limit -p bhard=<MAX_SESSION_SIZE>b <id>' <mountpoint>`, tagging the session's directory so any write exceeding the quota - from the API or from code running in the container - fails with `ENOSPC` (surfaced as a `413` from the API, or as a normal write error inside the container). This requires the API process to be able to run `xfs_quota -x`, which needs `CAP_SYS_ADMIN`; either grant it to the binary (`setcap cap_sys_admin+ep /usr/sbin/xfs_quota`) or scope a sudoers rule to it, rather than running the whole API as root.

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

Requests to `/health` are excluded from the access log.

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

`POST /execute` or `POST /sessions/{session_id}/execute` as `multipart/form-data`:

- `session_id` (path segment, only for `/sessions/{session_id}/execute`) - must reference a live session (`404` otherwise); if you instead call `POST /execute`, a throwaway session is created and destroyed for this call only
- `language` (text field) - one of `python`, `bash`, `javascript`, `c`, `cpp`, `java`, `csharp`, `rust`
- `code` (text field)
- `attachments` (optional file parts, filename = sub_path) - created/overwritten in the session before execution

Response is `multipart/mixed`: the first part is `application/json` -

```json
{"output": "...", "return_code": 0, "execution_time": 0.42, "timed_out": false, "deleted_files": []}
```

- followed by one file part per file created or modified during the run (`Content-Disposition: attachment; filename="<sub_path>"`).

Error statuses: `400` invalid input (bad language, invalid path), `404` missing session, `409` session lock timeout, `413` request/session/result limit, `503` unavailable capacity, `500` unexpected error fallback (including a failure to apply the session's XFS quota, when `SESSION_QUOTA_MOUNTPOINT` is configured).

## Local Harness

```cmd
python harness.py
python harness.py --api-url http://127.0.0.1:40003
python harness.py --check-quota --max-session-size 104857600
```

Runs a broad set of smoke checks against a running server: session/file lifecycle (`PUT`/`GET`/`DELETE`, seeded session creation), `/execute` file persistence and deletion detection across two calls sharing a `session_id`, execute attachments, ephemeral `/execute`, and error-path checks (unknown session, unsupported language, path traversal). It also verifies sandbox hardening: a symlink created by executed code is neither exposed as an attachment nor followed by the files API, a symlinked directory can't be used to escape the session root via `GET`/`PUT`, and the container's root filesystem is confirmed read-only.

`--check-quota` additionally verifies that `MAX_SESSION_SIZE` is enforced from *inside* the container by writing past it and expecting an `ENOSPC` failure - only meaningful once `SESSION_QUOTA_MOUNTPOINT` is configured and working (see above), so it's opt-in; pass `--max-session-size` to match the server's configured value if it differs from the default.

## Project Layout

- `app.py` - CLI entrypoint and server startup
- `harness.py` - local smoke-test script
- `code_executor_api/app_factory.py` - app wiring, startup, and cleanup hooks
- `code_executor_api/config.py` - environment-backed constants
- `code_executor_api/validation.py` - sub_path normalization, language/null-byte validation
- `code_executor_api/file_helpers.py` - size-limited streaming reads/writes shared by sessions and file uploads
- `code_executor_api/sessions.py` - `Session`/`SessionManager`: locking, creation/deletion, expiry sweep, and (when `SESSION_QUOTA_MOUNTPOINT` is set) per-session XFS project quota setup
- `code_executor_api/executor/docker_executor.py` - Docker container invocation and file-diffing
- `code_executor_api/routes/` - `/sessions`, `/sessions/{id}/files/{path}`, `/execute` (and `/sessions/{id}/execute`), `/health` handlers
- `executor_image/` - `Dockerfile` and per-language `executors/*.sh` scripts for the sandbox image (Python, Bash, Node.js, GCC/G++, JDK, .NET SDK, Rust toolchain)

## Dependencies

From `requirements.txt`:

- `aiohttp`
- `aiofiles`
