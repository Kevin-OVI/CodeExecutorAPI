# Code Executor API (aiohttp + Podman)

Code Executor API runs untrusted code inside hardened, ephemeral Podman containers (no capabilities, read-only root, resource limits) and exposes the result over HTTP. Callers manage a persistent **session** (a server-side working directory) so files can be created, read, and deleted across multiple executions without re-uploading a whole directory snapshot each time.

## Features

- `aiohttp` API with session management (`/sessions`), per-file access and directory listing (`/sessions/{id}/files/{path}`), code execution (`/execute` and `/sessions/{id}/execute`), and `/health`
- Sandboxed execution via `podman run` with CPU/memory/pid/ulimit caps and a hard wall-clock timeout
- Supports python, bash, javascript, typescript, c, c++, java, c#, rust
- Sessions persist a working directory across executions, guarded by a per-session lock; idle sessions expire automatically
- `execute` reports what changed: created/modified files (returned as multipart attachments, capped at `MAX_RESULT_ATTACHMENTS` with the remainder named in `omitted_files`) and deleted files
- Per-session byte *and* inode limits enforced by the kernel via XFS project quotas, so runaway file creation fails inside the sandbox rather than breaking the API

## Requirements

- Python 3.12+
- Podman (the `podman` CLI must be on the API process's `PATH`)
- The sandbox image must be built before starting the API, using `Containerfile`, then warmed once
  **as the user that runs the API**
  ```bash
  podman build -t code_executor executor_image
  podman run --rm --userns=keep-id:uid=4000,gid=4000 code_executor true
  ```
  The warm-up matters. Because `--userns=keep-id` asks for a uid mapping that differs from the
  storage's default one, Podman has to re-chown the image the first time it is used that way
  (`storage-chown-by-maps`). For this image that is several GiB of I/O and minutes of wall clock -
  far longer than `EXECUTION_TIMEOUT`, so without the warm-up the first real execution just times
  out. The result is cached per (image, mapping), so every later run starts in well under a second;
  redo it after each rebuild. It also costs roughly the image's own size again on disk, since the
  original layers are kept alongside the remapped ones.

  Building as root does not avoid this: the chown happens in the *service account's* container
  storage, keyed to the mapping it asks for, so an image built or pulled by another user still pays
  it on first use. Rootless can't sidestep it with an idmapped volume either - a custom
  `--volume ...:idmap=uids=...` mapping needs privileges rootless does not have, and a plain
  `:idmap` maps the host user onto container uid `0`, not `CONTAINER_USER_ID`.

The API shells out to `podman run` as the user running the service, so that user needs a working
Podman setup (rootful, or rootless with lingering enabled if the service is not started from a
login session). Two rootless-specific notes:

- Session working directories are bind-mounted into the container, and the executor passes
  `--userns=keep-id:uid=<CONTAINER_USER_ID>,gid=<CONTAINER_USER_ID>` so the image's `appuser` maps
  onto the host service account. That gives session files a single owner on both sides of the mount:
  without it the two uids differ under rootless Podman and neither side can fully manage the other's
  files (the API cannot read back or clean up what executed code created, and uploads are not
  writable from inside the container). The id is pinned to `4000` in both the `Containerfile` and
  `CONTAINER_USER_ID`; keep them in sync. It is deliberately not `1000`, so that if the user
  namespace is ever lost the container's uid does not land on a real host account.
- Rootless additionally requires `/etc/subuid` and `/etc/subgid` ranges for the service account that
  are **wider than `CONTAINER_USER_ID`**, since Podman maps container ids below it into that range
  before mapping `CONTAINER_USER_ID` itself to the host user. The conventional 65536-wide allocation
  is ample; a hand-trimmed range is not.
- On SELinux-enforcing hosts, bind mounts need a relabel: append `:Z` to the `--volume` argument in
  the same file.

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
- `MAX_CODE_LENGTH` (default: `65536` bytes) - must stay below the kernel's `MAX_ARG_STRLEN` (128 KiB) since code is passed as a single `podman run` argv entry
- `MAX_SESSION_SIZE` (default: `104857600` bytes)
- `MAX_SESSION_ENTRIES` (default: `32768`) - maximum inodes (files, directories and symlinks alike) a session may hold, enforced as the XFS project quota's `ihard` alongside `MAX_SESSION_SIZE`; like the byte limit it only applies when `SESSION_QUOTA_MOUNTPOINT` is set. Creating past it fails inside the container the same way running out of disk does. Keep it comfortably above what a real workload installs - a scientific Python stack is roughly 15000 inodes
- `MAX_RESULT_ATTACHMENTS` (default: `256`) - maximum changed files returned as `/execute` response parts. Anything beyond that is named in the response's `omitted_files` and stays retrievable through the files API; execution itself is never failed over this
- `MAX_SESSIONS` (default: `64`)
- `MAX_CONCURRENT_EXECUTIONS` (default: `4`)
- `CONTAINER_PIDS_LIMIT` (default: `128`)
- `CONTAINER_USER_ID` (default: `4000`) - uid *and* gid of `appuser` inside `PODMAN_IMAGE`; must match the `Containerfile`, since `--userns=keep-id` maps the host service account onto it
- `CONTAINER_ULIMIT_NOFILE` (default: `1024`)
- `CONTAINER_ULIMIT_FSIZE` (default: `268435456` bytes)
- `CONTAINER_RELATIVE_NICENESS` (default: `5`)
- `CONTAINER_TMPFS_SIZE` (default: `64m`)
- `PODMAN_IMAGE` (default: `code_executor`)
- `PODMAN_CHECK_TIMEOUT_SECONDS` (default: `5`)
- `SESSION_INACTIVITY_TIMEOUT_SECONDS` (default: `1800`) - idle sessions are deleted after this long
- `SESSION_SWEEP_INTERVAL_SECONDS` (default: `60`) - how often the expiry sweep runs
- `SESSION_LOCK_WAIT_TIMEOUT_SECONDS` (default: `30`) - how long a request waits for a session's lock (or an execution slot) before returning `409`/`503`
- `SESSION_ROOT_DIRECTORY` (default: the system temporary directory) - base directory for session working directories
- `SESSION_QUOTA_MOUNTPOINT` (default: unset) - XFS mountpoint containing `SESSION_ROOT_DIRECTORY`; when set, `MAX_SESSION_SIZE` and `MAX_SESSION_ENTRIES` are enforced as hard, kernel-level XFS project quotas per session (see below). When unset, `MAX_SESSION_SIZE` is only enforced against host-mediated writes (`PUT`/seed/attachment uploads) and `MAX_SESSION_ENTRIES` not at all - code running inside the container can otherwise write past both, bounded only by `CONTAINER_ULIMIT_FSIZE` per file and `EXECUTION_TIMEOUT`
- `SESSION_QUOTA_COMMAND` (default: `xfs_quota`) - argv prefix used to run `xfs_quota`, split like a shell command line but exec'd directly. Set it to `sudo -n /usr/sbin/xfs_quota` when the API runs unprivileged and `CAP_SYS_ADMIN` is delegated through a scoped sudoers rule (see below)

Running a second (e.g. test) deployment means pointing a separate process at a separate `PORT`/`PODMAN_IMAGE` via its own environment.

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

`SESSION_QUOTA_MOUNTPOINT` must be the XFS **mount point**, spelled exactly as it appears in `/proc/mounts` - not the session directory, and not any other directory on that filesystem. `xfs_quota` addresses a filesystem, so pointing it at a plain directory fails, and session creation returns `500`. The two variables are independent: mounting the filesystem directly at the session directory is equally valid, in which case they are the same path.

```
# filesystem mounted at the session directory itself
# /dev/loop0 /var/lib/code_executor/sessions xfs rw,...,prjquota 0 0
SESSION_ROOT_DIRECTORY=/var/lib/code_executor/sessions
SESSION_QUOTA_MOUNTPOINT=/var/lib/code_executor/sessions
```

Confirm the value is right before starting the service - `xfs_quota -x -c 'state' "$SESSION_QUOTA_MOUNTPOINT"` should report `Project quota state: ON` and, under `Accounting/Enforcement`, `ON` rather than the accounting-only state that `pqnoenforce` produces.

On session creation, `SessionManager` allocates a project id and runs `xfs_quota -x -c 'project -s -p <dir> <id>' -c 'limit -p bhard=<MAX_SESSION_SIZE in KiB>k ihard=<MAX_SESSION_ENTRIES> <id>' <mountpoint>`, tagging the session's directory so any write exceeding either quota - from the API or from code running in the container - fails with `ENOSPC` (surfaced as a `413` from the API, or as a normal write error inside the container). XFS reports project quotas as `ENOSPC` rather than `EDQUOT`, and does so for both limits, so exhausting the inode allowance is indistinguishable from filling the byte allowance without consulting `xfs_quota report`. This requires the API process to be able to run `xfs_quota -x`, which needs `CAP_SYS_ADMIN`. Do not run the whole API as root for it: after the identity-mapping change above, quota setup is the *only* thing left that wants privilege, so granting root to the entire service - which parses untrusted multipart input and handles filenames chosen by executed code - buys one subprocess call at the price of making any API bug a root compromise.

Delegate just that call instead, with a sudoers rule scoped to the binary:

```
codeexec ALL=(root) NOPASSWD: /usr/sbin/xfs_quota -x -c *
```

then point the service at it:

```
SESSION_QUOTA_COMMAND="sudo -n /usr/sbin/xfs_quota"
```

`SESSION_QUOTA_COMMAND` is an argv prefix, split with `shlex` and exec'd directly, so no shell is involved. `-n` makes sudo fail immediately rather than blocking on a password prompt. The residual grant is real but bounded: the service account can administer quotas on that filesystem, and nothing else.

Prefer this over `setcap cap_sys_admin+ep /usr/sbin/xfs_quota`. File capabilities need no `SESSION_QUOTA_COMMAND` change, but they attach to the binary for *every* local user, handing `CAP_SYS_ADMIN` to anyone with a shell on the box - a much wider grant than the sudoers rule for the same benefit.

Both hard limits are verified after they are set, because `limit` can exit 0 without registering anything (notably when the filesystem is mounted `pqnoenforce` instead of `prjquota` - in that case quotas are accounted and reported but never enforced, which no amount of verification can detect; check `/proc/mounts`).

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
curl http://127.0.0.1:40003/sessions/{session_id}/files/
curl -X PUT --data-binary @localfile.txt http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
curl -X DELETE http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
```

- `GET`/`PUT`/`DELETE` on a file return `404` if the session or file doesn't exist.
- `PUT` creates or overwrites the file (parent directories are created as needed); the request body is the raw file bytes.
- `GET` on a **directory** returns a JSON listing of that one level instead of file bytes; an empty path lists the session root. Unlike execution results the listing hides nothing - hidden directories and symlinks are included - so it is the way to discover files that `/execute` excluded or omitted. Symlinks are reported, never followed.

```json
{"path": "some", "entries": [
  {"name": "path.txt", "type": "file",      "size": 12,   "modified_at": 1757684400.123},
  {"name": "nested",   "type": "directory", "size": 4096, "modified_at": 1757684400.5}
]}
```

`type` is one of `file`, `directory`, `symlink` or `other`.

### Execute code

`POST /execute` or `POST /sessions/{session_id}/execute` as `multipart/form-data`:

- `session_id` (path segment, only for `/sessions/{session_id}/execute`) - must reference a live session (`404` otherwise); if you instead call `POST /execute`, a throwaway session is created and destroyed for this call only
- `language` (text field) - one of `python`, `bash`, `javascript`, `typescript`, `c`, `cpp`, `java`, `csharp`, `rust`
  - `javascript` accepts both CommonJS and ES module syntax (Node resolves the module type from the code itself) and supports top-level `await`
  - `typescript` is transpiled by `tsx`, which strips types without checking them, so a type error surfaces as a runtime failure rather than blocking the run; it executes as CommonJS, accepting `require`, `import`, `enum` and `namespace`, but not top-level `await`
  - both resolve relative paths and module specifiers against the session directory, so code can read and import files already in the session
- `code` (text field)
- `attachments` (optional file parts, filename = sub_path) - created/overwritten in the session before execution

Response is `multipart/mixed`: the first part is `application/json` -

```json
{"output": "...", "return_code": 0, "execution_time": 0.42, "timed_out": false, "deleted_files": [], "omitted_files": []}
```

- followed by one file part per file created or modified during the run, capped at `MAX_RESULT_ATTACHMENTS`.

Each attachment part carries its session sub_path in the RFC 5987 extended parameter, with a flattened ASCII `filename` for clients that only understand that one:

```
Content-Disposition: attachment; name="attachments"; filename="out_file.txt"; filename*=UTF-8''out%2Ffile.txt
```

Read `filename*` (aiohttp's `part.filename` already prefers it) to get the exact sub_path, matching the raw sub_paths in `deleted_files` and `omitted_files`. A plain `filename` cannot carry one: RFC 6266 has recipients strip directory components, and percent-escapes have no defined meaning there. `filename*` also keeps names containing quotes, newlines or non-ASCII characters - all of which executed code can create - from altering the header.

Notes on what counts as changed:

- `omitted_files` names the changed files that did not fit under `MAX_RESULT_ATTACHMENTS` (or that could not be read back). The run still succeeded; fetch them individually with `GET /sessions/{id}/files/{path}`. `deleted_files` is never truncated.
- Change detection compares `(inode, size, mtime, ctime)` rather than hashing contents, so rewriting a file with byte-identical content counts as a modification and comes back as an attachment.
- Hidden directories are excluded at every depth. The session directory is also the container's `$HOME`, so `.cache`, `.local`, `.npm` and the like would otherwise flood the response with package-manager noise. They still occupy the session's byte and inode quotas, and are still visible through the files API.

Error statuses: `400` invalid input (bad language, invalid path), `404` missing session, `409` session lock timeout, `413` request/session limit, `503` unavailable capacity, `500` unexpected error fallback (including a failure to apply the session's XFS quota, when `SESSION_QUOTA_MOUNTPOINT` is configured).

## Local Harness

```cmd
python harness.py
python harness.py --api-url http://127.0.0.1:40003
python harness.py --check-quota --max-session-size 104857600 --max-session-entries 32768
```

Runs a broad set of smoke checks against a running server: session/file lifecycle (`PUT`/`GET`/`DELETE`, seeded session creation), `/execute` file persistence and deletion detection across two calls sharing a `session_id`, execute attachments, directory listing, ephemeral `/execute`, and error-path checks (unknown session, unsupported language, path traversal). It also verifies sandbox hardening: a symlink created by executed code is neither exposed as an attachment nor followed by the files API, a symlinked directory can't be used to escape the session root via `GET`/`PUT`, and the container's root filesystem is confirmed read-only.

Three checks cover result collection specifically, each on its own session: repeated no-op runs must report nothing as changed for both API-uploaded and container-created files (this is what catches a broken `--userns=keep-id` mapping); hidden `$HOME` directories must be excluded from attachments while staying reachable through the files API; and creating more than `MAX_RESULT_ATTACHMENTS` files must yield a capped, still-successful response whose `omitted_files` are individually retrievable. Pass `--max-result-attachments` if the server's value differs from the default.

`--check-quota` additionally verifies that `MAX_SESSION_SIZE` and `MAX_SESSION_ENTRIES` are enforced from *inside* the container, by writing past each and expecting an `ENOSPC` failure - only meaningful once `SESSION_QUOTA_MOUNTPOINT` is configured and working (see above), so it's opt-in; pass `--max-session-size`/`--max-session-entries` to match the server's configured values if they differ from the defaults.

## Project Layout

- `app.py` - CLI entrypoint and server startup
- `harness.py` - local smoke-test script
- `code_executor_api/app_factory.py` - app wiring, startup, and cleanup hooks
- `code_executor_api/config.py` - environment-backed constants
- `code_executor_api/validation.py` - sub_path normalization, language/null-byte validation
- `code_executor_api/file_helpers.py` - size-limited streaming reads/writes shared by sessions and file uploads
- `code_executor_api/sessions.py` - `Session`/`SessionManager`: locking, creation/deletion, expiry sweep, and (when `SESSION_QUOTA_MOUNTPOINT` is set) per-session XFS project quota setup
- `code_executor_api/executor/podman_executor.py` - Podman container invocation and file-diffing
- `code_executor_api/routes/` - `/sessions`, `/sessions/{id}/files/{path}`, `/execute` (and `/sessions/{id}/execute`), `/health` handlers
- `executor_image/` - `Containerfile` and per-language `executors/*.sh` scripts for the sandbox image (Python, Bash, Node.js, GCC/G++, JDK, .NET SDK, Rust toolchain)

## Dependencies

From `requirements.txt`:

- `aiohttp`
- `aiofiles`
