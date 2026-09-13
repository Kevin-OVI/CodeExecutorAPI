"""Every tunable the API has, read from the environment once at import.

Import time is deliberate: a malformed setting raises here and the process never starts,
rather than becoming a 500 on whichever request first touches it. Importers bind these as
plain constants, so changing a setting means restarting the server.

Trailing comments give the unit, and name whatever outside this file a value has to agree
with - the Containerfile's uid, the kernel's argv limit, the XFS quota.
"""

import os
import shlex


def _read_argv_env(name: str, default: str) -> tuple[str, ...]:
    """Read a command line into an argv tuple, so it can be exec'd without a shell."""
    try:
        argv = tuple(shlex.split(_read_str_env(name, default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid command line: {exc}") from exc
    if not argv:
        raise ValueError(f"{name} must not be empty.")
    return argv


def _read_str_env(name: str, default: str) -> str:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip()


def _read_optional_str_env(name: str) -> str | None:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty.")
    return value


def _read_optional_path_env(name: str) -> str | None:
    """Read an optional path and anchor it, so it survives being handed to a child process.

    A relative setting is resolved against the API's own working directory at import time,
    which is what it already meant implicitly - but session paths are passed to `podman`,
    which runs with its own working directory, and a relative one would resolve elsewhere.
    """
    value = _read_optional_str_env(name)
    return value if value is None else os.path.abspath(value)


def _read_int_env(name: str, default: int, *, min_value: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc

    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}.")

    return value


HOST = _read_str_env("HOST", "127.0.0.1")  # loopback by default: the API is unauthenticated, and the sandbox has a routable path back to the host, so binding wider is an explicit decision (see "Hardening" in the README)
PORT = _read_int_env("PORT", 40003, min_value=1)

EXECUTION_TIMEOUT = _read_int_env("EXECUTION_TIMEOUT", 20, min_value=1)  # seconds
MAX_MEMORY = _read_str_env("MAX_MEMORY", "256M")
MAX_CPU_CORES = _read_int_env("MAX_CPU_CORES", 1, min_value=1)
MAX_OUTPUT_SIZE = _read_int_env("MAX_OUTPUT_SIZE", 10 * 1024 * 1024, min_value=1)  # bytes
MAX_CODE_LENGTH = _read_int_env("MAX_CODE_LENGTH", 64 * 1024, min_value=1)  # bytes, must stay below the kernel's MAX_ARG_STRLEN (128 KiB) since code is passed as a single `podman run` argv entry
MAX_SESSION_SIZE = _read_int_env("MAX_SESSION_SIZE", 100 * 1024 * 1024, min_value=1)  # bytes
MAX_SESSION_ENTRIES = _read_int_env("MAX_SESSION_ENTRIES", 32768, min_value=1)  # inodes (files + directories + symlinks), enforced as the XFS project quota's `ihard` and therefore only when SESSION_QUOTA_MOUNTPOINT is set
MAX_RESULT_ATTACHMENTS = _read_int_env("MAX_RESULT_ATTACHMENTS", 256, min_value=1)  # changed files returned as `/execute` response parts; the rest are listed in `omitted_files`
MAX_SESSIONS = _read_int_env("MAX_SESSIONS", 64, min_value=1)
MAX_CONCURRENT_EXECUTIONS = _read_int_env("MAX_CONCURRENT_EXECUTIONS", 4, min_value=1)
CONTAINER_PIDS_LIMIT = _read_int_env("CONTAINER_PIDS_LIMIT", 128, min_value=1)
CONTAINER_USER_ID = _read_int_env("CONTAINER_USER_ID", 4000, min_value=1)  # uid *and* gid of `appuser` in PODMAN_IMAGE; must match the Containerfile, since `--userns=keep-id` maps the host service account onto it
CONTAINER_ULIMIT_NOFILE = _read_int_env("CONTAINER_ULIMIT_NOFILE", 1024, min_value=1)
CONTAINER_ULIMIT_FSIZE = _read_int_env("CONTAINER_ULIMIT_FSIZE", 256 * 1024 * 1024, min_value=1)  # bytes
CONTAINER_RELATIVE_NICENESS = _read_int_env("CONTAINER_RELATIVE_NICENESS", 5)
CONTAINER_TMPFS_SIZE = _read_str_env("CONTAINER_TMPFS_SIZE", "64m")
CONTAINER_NETWORK = _read_str_env("CONTAINER_NETWORK", "bridge")  # podman network for executed code; point it at a dedicated network firewalled off the API's own port to keep outbound access while denying the sandbox a route back to the API (see "Hardening" in the README)
PODMAN_IMAGE = _read_str_env("PODMAN_IMAGE", "code_executor")
PODMAN_CHECK_TIMEOUT_SECONDS = _read_int_env("PODMAN_CHECK_TIMEOUT_SECONDS", 5, min_value=1)

SESSION_INACTIVITY_TIMEOUT_SECONDS = _read_int_env("SESSION_INACTIVITY_TIMEOUT_SECONDS", 1800, min_value=1)
SESSION_SWEEP_INTERVAL_SECONDS = _read_int_env("SESSION_SWEEP_INTERVAL_SECONDS", 60, min_value=1)
SESSION_LOCK_WAIT_TIMEOUT_SECONDS = _read_int_env("SESSION_LOCK_WAIT_TIMEOUT_SECONDS", 30, min_value=1)
SESSION_ROOT_DIRECTORY = _read_optional_path_env("SESSION_ROOT_DIRECTORY")  # absolute: session directories are bind-mounted by podman, which does not share this process's working directory
SESSION_QUOTA_MOUNTPOINT = _read_optional_str_env("SESSION_QUOTA_MOUNTPOINT")  # XFS mountpoint (containing SESSION_ROOT_DIRECTORY) to enforce MAX_SESSION_SIZE via a per-session XFS project quota; unset disables quota enforcement
SESSION_QUOTA_COMMAND = _read_argv_env("SESSION_QUOTA_COMMAND", "xfs_quota")  # argv prefix used to run xfs_quota; set to e.g. "sudo -n /usr/sbin/xfs_quota" when the API runs unprivileged and CAP_SYS_ADMIN is delegated through a scoped sudoers rule
