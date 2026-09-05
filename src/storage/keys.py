from pathlib import PurePosixPath
from urllib.parse import unquote


def normalize_storage_key(key: str) -> str:
    """Return a safe, relative POSIX object key (including encoded traversal checks)."""
    value = (key or "").strip()
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("invalid storage key")
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    for candidate in (value, decoded):
        if candidate.startswith("/") or "\\" in candidate or "\x00" in candidate:
            raise ValueError("invalid storage key")
        if any(part in {"", ".", ".."} for part in candidate.split("/")):
            raise ValueError("invalid storage key")
    return PurePosixPath(*value.split("/")).as_posix()


def normalize_prefix(prefix: str) -> str:
    value = (prefix or "").strip().strip("/")
    return normalize_storage_key(value) if value else ""
