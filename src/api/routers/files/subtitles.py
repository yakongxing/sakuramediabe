from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import verify_subtitle_signature
from src.common.file_signatures import resolve_subtitle_storage_key
from src.storage import StorageNotFound, asset_storage

router = APIRouter(prefix="/files/subtitles", tags=["files"])


@router.get("/{subtitle_id}", include_in_schema=False)
def get_subtitle_file(
    request: Request,
    subtitle_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    require_signed_params(expires, signature)

    verify_subtitle_signature(subtitle_id, expires, signature)
    key = resolve_subtitle_storage_key(subtitle_id)
    storage = asset_storage()
    local_path = storage.local_path(key)
    if local_path is not None:
        require_existing_file(local_path)
        return FileResponse(local_path, media_type="text/plain; charset=utf-8")
    try:
        return storage.range_response(key, request.headers.get("range"), "text/plain; charset=utf-8")
    except StorageNotFound as exc:
        from src.api.exception.errors import ApiError
        raise ApiError(404, "file_not_found", "文件不存在") from exc
