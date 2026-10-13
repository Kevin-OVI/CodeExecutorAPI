"""Which image each supported language runs in, and the command that runs it.

The single source of truth for the language list: validation accepts exactly these keys, and the
executor reads the image and command from here. It is a module of its own because validation
cannot import the executor, which already imports validation through `sessions`.
"""

from typing import NamedTuple


class LanguageRuntime(NamedTuple):
    image: str  # environment suffix: the language runs in f"{PODMAN_IMAGE_PREFIX}-{image}", as tagged by executor_images/build.sh
    command: tuple[str, ...]  # argv the code is appended to as a single final argument


LANGUAGES: dict[str, LanguageRuntime] = {
    "python": LanguageRuntime("python", ("python", "-c")),
    "bash": LanguageRuntime("bash", ("bash", "-c")),
    "javascript": LanguageRuntime("node", ("node", "--eval")),
    "typescript": LanguageRuntime("node", ("tsx", "--eval")),
    "c": LanguageRuntime("gcc", ("/executors/c.sh",)),
    "cpp": LanguageRuntime("gcc", ("/executors/cpp.sh",)),
    "java": LanguageRuntime("java", ("/executors/java.sh",)),
    "csharp": LanguageRuntime("dotnet", ("/executors/csharp.sh",)),
    "rust": LanguageRuntime("rust", ("/executors/rust.sh",)),
}
