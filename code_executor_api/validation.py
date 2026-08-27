import json
import posixpath

from aiohttp import hdrs
from aiohttp.web import HTTPBadRequest

SUPPORTED_LANGUAGES = frozenset(("python", "bash", "javascript", "c", "cpp", "java", "csharp", "rust"))


class ValidationError(HTTPBadRequest):
    def __init__(self, message: str, headers=None):
        if headers is None:
            headers = {}
        headers[hdrs.CONTENT_TYPE] = "application/json"
        super().__init__(headers=headers, text=json.dumps({"error": message}))
        self.message = message


def normalize_sub_path(filename: str) -> str:
    normalized = posixpath.normpath(filename.replace("\\", "/").lstrip("/"))
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        raise ValidationError("Invalid file path: cannot access a path higher than the root")
    return normalized


def validate_language(language: str | None):
    if language is None:
        raise ValidationError("Missing 'language' field")
    if language not in SUPPORTED_LANGUAGES:
        raise ValidationError(f"Language {language} is unsupported")


def validate_code(code: str | None):
    if code is None:
        raise ValidationError("Missing 'code' field")
    if "\x00" in code:
        raise ValidationError("Null bytes are not allowed in code")
