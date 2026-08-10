import os


def _read_str_env(name: str, default: str) -> str:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty.")
    return value


def _read_optional_str_env(name: str) -> str | None:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty.")
    return value


def _read_int_env(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc

    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}.")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}.")

    return value


HOST = _read_str_env("HOST", "127.0.0.1")
PORT = _read_int_env("PORT", 40003, min_value=1, max_value=65535)
API_TOKEN = _read_optional_str_env("API_TOKEN")
if API_TOKEN is not None and len(API_TOKEN) < 32:
    raise ValueError("API_TOKEN must contain at least 32 characters.")

EXECUTION_TIMEOUT = _read_int_env("EXECUTION_TIMEOUT", 20, min_value=1)  # seconds
MAX_MEMORY = _read_str_env("MAX_MEMORY", "256M")
MAX_CPU_CORES = _read_int_env("MAX_CPU_CORES", 1, min_value=1)
MAX_OUTPUT_SIZE = _read_int_env("MAX_OUTPUT_SIZE", 10 * 1024 * 1024, min_value=1)  # bytes
MAX_REQUEST_SIZE = _read_int_env("MAX_REQUEST_SIZE", 16 * 1024 * 1024, min_value=1)  # bytes
MAX_SESSION_SIZE = _read_int_env("MAX_SESSION_SIZE", 100 * 1024 * 1024, min_value=1)  # bytes
MAX_SESSION_FILES = _read_int_env("MAX_SESSION_FILES", 1000, min_value=1)
MAX_SESSIONS = _read_int_env("MAX_SESSIONS", 64, min_value=1)
MAX_CONCURRENT_EXECUTIONS = _read_int_env("MAX_CONCURRENT_EXECUTIONS", 4, min_value=1)
CONTAINER_PIDS_LIMIT = _read_int_env("CONTAINER_PIDS_LIMIT", 128, min_value=1)
CONTAINER_ULIMIT_NOFILE = _read_int_env("CONTAINER_ULIMIT_NOFILE", 1024, min_value=1)
CONTAINER_ULIMIT_FSIZE = _read_int_env("CONTAINER_ULIMIT_FSIZE", 10 * 1024 * 1024, min_value=1)  # bytes
CONTAINER_RELATIVE_NICENESS = _read_int_env("CONTAINER_RELATIVE_NICENESS", 5, min_value=0, max_value=19)
CONTAINER_TMPFS_SIZE = _read_str_env("CONTAINER_TMPFS_SIZE", "64m")
DOCKER_IMAGE = _read_str_env("DOCKER_IMAGE", "code_executor")
DOCKER_CHECK_TIMEOUT_SECONDS = _read_int_env("DOCKER_CHECK_TIMEOUT_SECONDS", 5, min_value=1)

SESSION_INACTIVITY_TIMEOUT_SECONDS = _read_int_env("SESSION_INACTIVITY_TIMEOUT_SECONDS", 1800, min_value=1)
SESSION_SWEEP_INTERVAL_SECONDS = _read_int_env("SESSION_SWEEP_INTERVAL_SECONDS", 60, min_value=1)
SESSION_LOCK_WAIT_TIMEOUT_SECONDS = _read_int_env("SESSION_LOCK_WAIT_TIMEOUT_SECONDS", 30, min_value=1)
SESSION_ROOT_DIRECTORY = _read_optional_str_env("SESSION_ROOT_DIRECTORY")
