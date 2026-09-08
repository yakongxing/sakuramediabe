import ipaddress
import re
from urllib.parse import urlsplit

from src.storage.keys import normalize_storage_key

_URL_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HTTP_URL_PREFIX = re.compile(r"^https?://", re.IGNORECASE)
_ASCII_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_EXTERNAL_IMAGE_URL_BYTES = 2048


def _is_url_like(value: str) -> bool:
    return value.startswith("//") or _URL_SCHEME_PREFIX.match(value) is not None


def _validate_hostname(hostname: str, authority: str) -> None:
    if any(character.isspace() for character in authority):
        raise ValueError("external image URL authority must not contain whitespace")
    if "%" in authority or "\\" in authority:
        raise ValueError("external image URL authority contains invalid characters")

    if authority.startswith("["):
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError as exc:
            raise ValueError("external image URL has an invalid IPv6 host") from exc
        return

    candidate = hostname.removesuffix(".")
    if not candidate:
        raise ValueError("external image URL has an invalid hostname")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("external image URL has an invalid hostname") from exc
    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(
        not _ASCII_HOST_LABEL.fullmatch(label) for label in labels
    ):
        raise ValueError("external image URL has an invalid hostname")


def validate_external_image_url(value: str) -> str:
    """Validate a provider image URL without normalizing or probing it."""
    if not value:
        raise ValueError("external image URL must not be empty")
    try:
        encoded_value = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("external image URL contains invalid Unicode") from exc
    if len(encoded_value) > _MAX_EXTERNAL_IMAGE_URL_BYTES:
        raise ValueError("external image URL is too long")
    if any(character.isspace() for character in value):
        raise ValueError("external image URL must not contain whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("external image URL contains control characters")
    if "\\" in value:
        raise ValueError("external image URL must not contain backslashes")
    if _MALFORMED_PERCENT_ESCAPE.search(value):
        raise ValueError("external image URL contains a malformed percent escape")
    if not _HTTP_URL_PREFIX.match(value):
        raise ValueError("external image URL must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("external image URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("external image URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("external image URL must not contain credentials")
    if "#" in value:
        raise ValueError("external image URL must not contain a fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("external image URL has an invalid port") from exc
    authority = parsed.netloc
    if authority.endswith(":"):
        raise ValueError("external image URL has an invalid port")
    host_authority = authority.rsplit("@", 1)[-1]
    if host_authority.startswith("["):
        host_authority = host_authority.split("]", 1)[0] + "]"
    else:
        host_authority = host_authority.rsplit(":", 1)[0]
    _validate_hostname(parsed.hostname, host_authority)
    return value


def is_external_image_reference(value: str | None) -> bool:
    if not value:
        return False
    try:
        validate_external_image_url(value)
    except ValueError:
        return False
    return True


def is_nonlocal_image_reference(value: str | None) -> bool:
    """Return false only for a canonical, safe internal storage key."""
    if not isinstance(value, str) or not value:
        return True
    try:
        value.encode("utf-8")
        if any(
            ord(character) < 32 or 127 <= ord(character) <= 159
            for character in value
        ):
            return True
        if _is_url_like(value) or "://" in value:
            return True
        normalized = normalize_storage_key(value)
    except Exception:
        return True
    return normalized != value
