# Code Executor API (aiohttp + Podman)

Code Executor API runs untrusted code inside hardened, ephemeral Podman containers (no capabilities, read-only root, resource limits) and exposes the result over HTTP. Callers manage a persistent **session** (a server-side working directory) so files can be created, read, and deleted across multiple executions without re-uploading a whole directory snapshot each time.

Linux only: the sandbox depends on rootless Podman, and the per-session limits on XFS project quotas.

[Requirements](#requirements) · [Setup](#setup) · [Run API](#run-api) · [Configuration](#configuration) · [API](#api) · [Local Harness](#local-harness) · [Hardening](#hardening) · [Project Layout](#project-layout) · [Dependencies](#dependencies)

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
  `code_executor_api/executor/podman_executor.py`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run API

Default host/port (from env or defaults):

```bash
python app.py
```

Override host/port from CLI:

```bash
python app.py --host 127.0.0.1 --port 40003
```

Startup fails with a missing-variable error when `SESSION_QUOTA_MOUNTPOINT` is not set, since nothing else enforces `MAX_SESSION_SIZE`/`MAX_SESSION_ENTRIES`. Pass `--no-session-quota` to start anyway - a development convenience that logs a warning and leaves both limits unenforced:

```bash
python app.py --no-session-quota
```

## Configuration

Read from the environment at import (see `code_executor_api/config.py`). A malformed value fails startup rather than surfacing later as a `500`, and nothing re-reads the environment afterwards, so changing one means restarting the server.

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `127.0.0.1` | Loopback, because the API is unauthenticated and executed code can route back to the host. See [Hardening](#hardening) before binding wider |
| `PORT` | `40003` | |
| `EXECUTION_TIMEOUT` | `20` | Seconds of wall clock per execution |
| `MAX_MEMORY` | `256M` | |
| `MAX_CPU_CORES` | `1` | |
| `MAX_OUTPUT_SIZE` | `10485760` | Bytes; output past this is truncated, not failed |
| `MAX_CODE_LENGTH` | `65536` | Bytes; must stay under the kernel's `MAX_ARG_STRLEN` (128 KiB), since code is passed as one `podman run` argv entry |
| `MAX_SESSION_SIZE` | `104857600` | Bytes per session, enforced only by the XFS quota - see [Enforcing `MAX_SESSION_SIZE`](#enforcing-max_session_size-with-an-xfs-project-quota) |
| `MAX_SESSION_ENTRIES` | `32768` | Inodes per session (files, directories and symlinks alike), enforced the same way. Keep it well clear of what a real workload installs - a scientific Python stack is roughly 15000 |
| `MAX_RESULT_ATTACHMENTS` | `256` | Changed files returned as `/execute` parts; the rest are named in `omitted_files`, and execution is never failed over it |
| `MAX_SESSIONS` | `64` | |
| `MAX_CONCURRENT_EXECUTIONS` | `4` | |
| `CONTAINER_PIDS_LIMIT` | `128` | |
| `CONTAINER_USER_ID` | `4000` | uid *and* gid of `appuser` inside `PODMAN_IMAGE`; must match the `Containerfile` |
| `CONTAINER_ULIMIT_NOFILE` | `1024` | |
| `CONTAINER_ULIMIT_FSIZE` | `268435456` | Bytes, per individual file |
| `CONTAINER_RELATIVE_NICENESS` | `5` | Added to the API process's own niceness |
| `CONTAINER_TMPFS_SIZE` | `64m` | Size of the container's `/tmp` |
| `CONTAINER_NETWORK` | `bridge` | Podman network for executed code. The default reaches the host - see [Hardening](#hardening). Must be one that gets its own namespace: `host` makes `podman run` fail outright, because the container is also given `--sysctl net.ipv4.ping_group_range` (see [`ping` needs no capability](#ping-needs-no-capability)) |
| `PODMAN_IMAGE` | `code_executor` | |
| `PODMAN_CHECK_TIMEOUT_SECONDS` | `5` | |
| `SESSION_INACTIVITY_TIMEOUT_SECONDS` | `1800` | Seconds before an idle session is deleted |
| `SESSION_SWEEP_INTERVAL_SECONDS` | `60` | Seconds between expiry sweeps |
| `SESSION_LOCK_WAIT_TIMEOUT_SECONDS` | `30` | Seconds a request waits for a session lock or an execution slot before returning `409`/`503` |
| `SESSION_ROOT_DIRECTORY` | system temp dir | Base directory for session working directories |
| `SESSION_QUOTA_MOUNTPOINT` | unset | XFS mount point containing `SESSION_ROOT_DIRECTORY`. Unset leaves both session limits unenforced, which the server refuses to start under without `--no-session-quota` - see [Enforcing `MAX_SESSION_SIZE`](#enforcing-max_session_size-with-an-xfs-project-quota) |
| `SESSION_QUOTA_COMMAND` | `xfs_quota` | argv prefix for `xfs_quota`, split like a shell command line but exec'd directly. Set it to `sudo -n /usr/sbin/xfs_quota` to delegate `CAP_SYS_ADMIN` without running the API as root - see [Enforcing `MAX_SESSION_SIZE`](#enforcing-max_session_size-with-an-xfs-project-quota) |

Running a second (e.g. test) deployment means pointing a separate process at a separate `PORT`/`PODMAN_IMAGE` via its own environment.

## API

### Health check

```bash
curl http://127.0.0.1:40003/health
```

Requests to `/health` are excluded from the access log.

### Sessions

Create a session (optionally seeding files via multipart, filename = relative sub_path):

```bash
curl -X POST http://127.0.0.1:40003/sessions
```

Response: `{"session_id": "..."}`

Delete a session immediately:

```bash
curl -X DELETE http://127.0.0.1:40003/sessions/{session_id}
```

### Session files

```bash
curl http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
curl http://127.0.0.1:40003/sessions/{session_id}/files/
curl -X PUT --data-binary @localfile.txt http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
curl -X DELETE http://127.0.0.1:40003/sessions/{session_id}/files/some/path.txt
```

- `GET`/`PUT`/`DELETE` on a file return `404` if the session or file doesn't exist.
- `PUT` creates or overwrites the file (parent directories are created as needed); the request body is the raw file bytes.
- `PUT` (like seeded files and `/execute` attachments) returns `413` when the write is refused by the session's XFS quota or exceeds the per-file `CONTAINER_ULIMIT_FSIZE` cap. The body is streamed to a temporary file first, so a rejected write never leaves a truncated file behind. Without `SESSION_QUOTA_MOUNTPOINT` only the per-file cap applies, and a session's total size is unbounded.
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

- `omitted_files` names the changed files that did not fit under `MAX_RESULT_ATTACHMENTS` (or that could not be read back, including filenames not representable as UTF-8). The run still succeeded. Readable files in a live persistent session can be fetched individually with `GET /sessions/{id}/files/{path}`; a throwaway `/execute` session is destroyed after the call, so its omitted files cannot be fetched later. `deleted_files` is never truncated.
- Change detection compares `(inode, size, mtime, ctime)` rather than hashing contents, so rewriting a file with byte-identical content counts as a modification and comes back as an attachment.
- Hidden directories are excluded at every depth. The session directory is also the container's `$HOME`, so `.cache`, `.local`, `.npm` and the like would otherwise flood the response with package-manager noise. They still occupy the session's byte and inode quotas, and are still visible through the files API.

Error statuses: `400` invalid input (bad language, invalid path), `404` missing session, `409` session lock timeout, `413` request/session limit, `503` unavailable capacity, `500` unexpected error fallback (including a failure to apply the session's XFS quota, when `SESSION_QUOTA_MOUNTPOINT` is configured).

## Local Harness

```bash
python harness.py
python harness.py --api-url http://127.0.0.1:40003
python harness.py --list
python harness.py --only results --skip-heavy
```

`harness.py` drives a running server over HTTP and asserts the behaviour this README documents. It has no dependencies beyond the ones the API already needs. Every check is an `assert`, so it refuses to run under `python -O`.

Checks are grouped, and `--list` prints the registry: each check's name, its group, whether it is heavy, and a one-line summary. `--only` and `--skip` take either a check name or a group name. A run executes every selected check even after one fails, prints a `PASS`/`FAIL`/`ERROR`/`SKIP` line for each, and ends with a summary, a non-zero exit code if anything failed, and the exact `--only` command to re-run just the failures. `-x` stops at the first failure instead; `--skip-heavy` drops the checks that stress the host (fork bombs, large allocations, multi-megabyte transfers).

Each check is handed its own freshly created session and its own is deleted afterwards, so checks neither depend on nor disturb one another and any one of them can be run alone.

### Always-on groups

| Group | What it covers |
|---|---|
| `core` | Session and file lifecycle, `PUT`/`GET`/`DELETE` including nested parents, directories, symlinks and zero-byte bodies, directory listings, sub_path containment, and that an interrupted `PUT` leaves the original file intact with no staging debris |
| `languages` | All nine languages: module styles for `javascript`, type syntax for `typescript` (and that it is CommonJS, so top-level `await` fails), and for each compiled language a hello world, a compile error with diagnostics, and the requirement that nothing is left in the session directory |
| `sandbox` | Read-only root filesystem, symlink containment, `RLIMIT_NOFILE`, and (heavy) that `MAX_MEMORY` kills rather than hangs, `CONTAINER_PIDS_LIMIT` bounds process creation without breaking the session, and `MAX_OUTPUT_SIZE` truncates without failing the run |
| `results` | Change detection including cases a naive `(size, mtime)` stamp would miss, the `MAX_RESULT_ATTACHMENTS` cap and its `omitted_files`, that `deleted_files` is never truncated, that a non-UTF-8 filename is omitted rather than fatal, and that awkward filenames (quotes, newlines, spaces, non-ASCII, nested paths) survive `Content-Disposition` exactly |
| `errors` | The documented status codes for malformed `/execute` and `/sessions` requests, and that a rejected request leaves neither attachments nor session slots behind |
| `network` | That the sandbox has the outbound access `CONTAINER_NETWORK` intends, and that executed code **cannot** reach the API that is running it |

The `network` group needs outbound access from the container; skip it with `--skip network` where there is none.

> `api_unreachable_from_sandbox` is worth reading the failure message of. The sandbox has a routable path back to the host, so a server bound beyond loopback is reachable from the code it executes - which can then exhaust `MAX_SESSIONS` and start further executions of its own, sidestepping the per-container limits. It cannot read other callers' files: session ids are not discoverable from inside. `HOST` defaults to `127.0.0.1` so this passes out of the box; if you bind wider, isolate `CONTAINER_NETWORK` instead. See [Hardening](#hardening).

### Opt-in groups

Each needs the server configured differently from production defaults, so each has its own flag and is skipped (loudly, with its requirement) otherwise. Start a second server with the tuned environment on its own port and point the harness at it. The value flags exist because nothing tells the harness what the server is configured to; if they disagree, the assertion messages say so.

| Flag | Server configuration | Harness flags |
|---|---|---|
| `--check-timeout` | `EXECUTION_TIMEOUT=8` | `--execution-timeout 8` |
| `--check-capacity` | `MAX_SESSIONS=4 MAX_CONCURRENT_EXECUTIONS=2` | `--max-sessions 4 --max-concurrent-executions 2` |
| `--check-lock` | `SESSION_LOCK_WAIT_TIMEOUT_SECONDS=2` | `--lock-wait-timeout 2` |
| `--check-sweep` | `SESSION_INACTIVITY_TIMEOUT_SECONDS=5 SESSION_SWEEP_INTERVAL_SECONDS=2` | `--inactivity-timeout 5 --sweep-interval 2` |
| `--check-fsize` | `CONTAINER_ULIMIT_FSIZE=1048576` | `--max-file-size 1048576` |
| `--check-quota` | `SESSION_QUOTA_MOUNTPOINT` on an XFS `prjquota` mount | `--max-session-size` / `--max-session-entries` |
| `--check-host` | none, but the harness must run on the API host | `--session-root <SESSION_ROOT_DIRECTORY>` |

```bash
EXECUTION_TIMEOUT=8 SESSION_LOCK_WAIT_TIMEOUT_SECONDS=2 python app.py --no-session-quota --port 40010
python harness.py --api-url http://127.0.0.1:40010 --only timeout lock --execution-timeout 8 --lock-wait-timeout 2
```

Naming a check or a group in `--only` enables its flag implicitly, so a single check can be run without also spelling out its gate. `--check-all` turns on every opt-in group, but no single server configuration satisfies all of them at once - `capacity` wants limits the other groups need headroom under, and `fsize` wants a per-file cap too small for `csharp` to build under - so it is for a purpose-built server, and it says so when used.

`--check-capacity` in particular should be run on its own. It works by saturating `MAX_SESSIONS`, and every other check creates a session of its own.

`--check-host` shells out to `podman` and looks at `SESSION_ROOT_DIRECTORY` directly, because what it checks is not visible over HTTP: that no container outlives the request that started it, that throwaway sessions leave no directories behind, and that `DELETE` really removes a session's directory even when executed code left something awkward in it.

### Running the quota checks on a development machine

`--check-quota` needs a real XFS filesystem with project quotas enabled, which a loopback image provides without a spare disk:

```bash
apt install xfsprogs
truncate -s 2G /var/tmp/code_executor_quota.img
mkfs.xfs /var/tmp/code_executor_quota.img
mkdir -p /var/lib/code_executor
mount -o loop,prjquota /var/tmp/code_executor_quota.img /var/lib/code_executor
mkdir -p /var/lib/code_executor/sessions
chown "$SERVICE_USER" /var/lib/code_executor/sessions
xfs_quota -x -c 'state' /var/lib/code_executor   # Project quota state: ON, Enforcement: ON
```

Then start the server with `SESSION_ROOT_DIRECTORY=/var/lib/code_executor/sessions` and `SESSION_QUOTA_MOUNTPOINT=/var/lib/code_executor`, and run `python harness.py --check-quota`. The mount does not survive a reboot, which is the point - it is a test fixture, not the deployment recipe in [Enforcing `MAX_SESSION_SIZE`](#enforcing-max_session_size-with-an-xfs-project-quota).

## Hardening

### The sandbox can reach whatever the host exposes on its bridge

Containers run with `--net=$CONTAINER_NETWORK`, which defaults to podman's `bridge`. That gives executed code a routable path to the host - `host.containers.internal`, `host.docker.internal` and the bridge gateway address all resolve from inside - so **anything the host listens on with a non-loopback binding is reachable from the code being executed**. That includes this API.

This is not derivable from the flag list, and it is the assumption the per-container limits are implicitly making: `--pids-limit`, `--memory` and `--cpus` bound *one* container, but code that can reach the API does not have to defeat any of them. It calls `POST /execute` and gets another container with a fresh budget. `MAX_CONCURRENT_EXECUTIONS` still caps the total, so there is a ceiling; what changes is that a single submission's resource envelope is no longer the per-container limits.

What it allows, stated precisely:

- **Denial of service.** `POST /sessions` until `MAX_SESSIONS` is exhausted, after which every legitimate caller gets `503`. Sessions expire after `SESSION_INACTIVITY_TIMEOUT_SECONDS`, so holding the condition just means re-creating them. `MAX_CONCURRENT_EXECUTIONS` can be saturated the same way. On disk that is `MAX_SESSIONS` × `MAX_SESSION_SIZE`, or unbounded when `SESSION_QUOTA_MOUNTPOINT` is unset.
- **Resource amplification**, as above.

What it does **not** allow is cross-session data access. There is no enumeration endpoint - every session-scoped route carries `{session_id}` in its path and nothing lists sessions. Session ids are 128 bits from a CSPRNG, so they are neither brute-forceable nor predictable from an id the caller legitimately owns. The id never reaches the container either: the working directory is an independently random `mkdtemp` name, so although `/proc/self/mountinfo` exposes the host path of the bind mount, the name it reveals is unrelated to the session id, and nothing passes the id in as an environment variable or argument. Reading another caller's files requires an id obtained some other way - a client leaking one, not the API disclosing it.

Note that none of this is sandbox-specific. An unauthenticated API bound to `0.0.0.0` answers anything that can route to the host - other containers, the LAN - and sandboxed code is simply one of those callers, not a privileged one.

### Closing it

Two independent controls. Neither depends on the other, and applying both is the intended production posture.

**1. Keep the API on loopback.** `HOST` defaults to `127.0.0.1`, so the default configuration is already closed. If you need remote callers, put a reverse proxy that terminates authentication in front of it rather than binding the API itself wider.

**2. Isolate the container network.** This is what covers the case where the API *must* bind beyond loopback, and it is worth doing regardless. Create a dedicated network:

```bash
podman network create code_executor_sandbox
podman network inspect code_executor_sandbox --format '{{range .Subnets}}{{.Subnet}}{{end}}'
```

Drop traffic from that subnet to the API's port (substitute the subnet printed above and your `PORT`):

```bash
sudo nft add table inet code_executor
sudo nft 'add chain inet code_executor input { type filter hook input priority 0; }'
sudo nft add rule inet code_executor input ip saddr 10.89.0.0/24 tcp dport 40003 drop
```

Then point the API at it:

```bash
export CONTAINER_NETWORK=code_executor_sandbox
```

This blocks only the API's port. The image's tooling (`requests`, `yt-dlp`, `dnsutils`) keeps its outbound access, which the harness's `outbound_network` check asserts.

Verify both with the harness - `api_unreachable_from_sandbox` probes the host aliases on the API's own port from inside a container:

```bash
python harness.py --only api_unreachable_from_sandbox outbound_network
```

### `ping` needs no capability

Containers run with `--cap-drop=ALL`, and `ping` is the one shipped utility that looks like a casualty of it. Its own error message says so:

```
ping: socktype: SOCK_RAW
ping: socket: Operation not permitted
ping: => missing cap_net_raw+p capability or setuid?
```

That message points at the wrong fix. iputils' `ping` opens an ICMP *datagram* socket first and only falls back to a raw socket when the kernel refuses it, and what refuses it here is podman's default `net.ipv4.ping_group_range` of `0 0` - root's gid only - meeting a container that runs as `CONTAINER_USER_ID`. Nothing about the capability set is involved. Widening the range to exactly that one gid is therefore the whole fix, and `podman_executor.py` passes it unconditionally:

```
--sysctl net.ipv4.ping_group_range=4000 4000
```

`--cap-add=NET_RAW` would also make `ping` work, which is what makes this worth writing down. Podman adds capabilities as **ambient** ones, so the grant is not scoped to `/bin/ping`: every process the executed code spawns holds `CAP_NET_RAW` effective, and with it `AF_PACKET` sockets. That is packet capture, ARP poisoning and source-address spoofing on a bridge shared with up to `MAX_CONCURRENT_EXECUTIONS` other sessions - so it would break the cross-session isolation claimed above, which rests on session ids being unguessable rather than on anything at the network layer. It would also make the nftables rule above (which matches on `ip saddr`) stop being a boundary, since forged frames can carry a source outside the sandbox subnet.

Note that `--security-opt=no-new-privileges` rules out the other obvious route as well: with `NO_NEW_PRIVS` set the kernel ignores file capabilities on `execve`, so `setcap cap_net_raw+ep /bin/ping` in the `Containerfile` would not work either.

The `ping_without_net_raw` check asserts both halves - that `ping` works *and* that every capability set is still empty - because either one alone passes under the wrong fix:

```bash
python harness.py --only ping_without_net_raw
```

Utilities that genuinely do stay unavailable under `--cap-drop=ALL`: `nmap`'s privileged scan types (`-sS`, `-sU`, `-O`, `--traceroute`; note that `--cap-add` would not help, as nmap gates those on `geteuid() == 0` rather than on capabilities - `-sT`, its non-root default, works), binding ports below 1024, `chown` to another uid, `mount`, `dmesg`, and raising ulimits. Everything else the image ships - `dig`, `netstat`, `arp`, `telnet`, `git`, `curl`, `wget` and the Python stack - is unaffected.

### Enforcing `MAX_SESSION_SIZE` with an XFS project quota

Without `SESSION_QUOTA_MOUNTPOINT`, `MAX_SESSION_SIZE` is not enforced at all - neither against files written through the API nor against what executed code writes directly into the session's mounted working directory - which is why the server refuses to start unless `--no-session-quota` says that is intended. The quota is the only mechanism that enforces it, so put `SESSION_ROOT_DIRECTORY` on an XFS filesystem with project quotas enabled:

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

## Project Layout

- `app.py` - CLI entrypoint and server startup
- `harness.py` - acceptance harness: drives a running server over HTTP (see [Local Harness](#local-harness))
- `code_executor_api/app_factory.py` - app wiring, startup, and cleanup hooks
- `code_executor_api/config.py` - environment-backed constants
- `code_executor_api/validation.py` - sub_path normalization, language/null-byte validation
- `code_executor_api/file_helpers.py` - streaming reads/writes (bounded per file) shared by sessions and file uploads
- `code_executor_api/sessions.py` - `Session`/`SessionManager`: locking, creation/deletion, expiry sweep, and (when `SESSION_QUOTA_MOUNTPOINT` is set) per-session XFS project quota setup
- `code_executor_api/executor/podman_executor.py` - Podman container invocation and file-diffing
- `code_executor_api/routes/` - `/sessions`, `/sessions/{id}/files/{path}`, `/execute` (and `/sessions/{id}/execute`), `/health` handlers
- `executor_image/` - `Containerfile` and per-language `executors/*.sh` scripts for the sandbox image (Python, Bash, Node.js, GCC/G++, JDK, .NET SDK, Rust toolchain)

## Dependencies

From `requirements.txt`:

- `aiohttp`
- `aiofiles`
