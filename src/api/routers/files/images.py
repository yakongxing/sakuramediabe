import mimetypes

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import build_signed_file_cache_control, verify_image_signature
from src.common.image_store import image_pack_path, read_image_bytes
from src.storage import StorageNotFound, asset_storage

router = APIRouter(prefix="/files/images", tags=["files"])

@router.get("/{file_path:path}", include_in_schema=False)
def get_image_file(
    request: Request,
    file_path: str,
    expires: int | None = None,
    signature: str | None = None,
):
    require_signed_params(expires, signature)

    normalized_path = verify_image_signature(file_path, expires, signature)
    storage = asset_storage()
    local_path = storage.local_path(normalized_path)
    if local_path is not None:
        pack_path = image_pack_path(normalized_path, storage=storage)
        if pack_path is not None and pack_path.is_file():
            try:
                content = read_image_bytes(normalized_path, storage=storage)
            except FileNotFoundError:
                content = None
            if content is not None:
                media_type, _ = mimetypes.guess_type(normalized_path)
                return Response(content=content, media_type=media_type or "application/octet-stream",
                                headers={"Cache-Control": build_signed_file_cache_control(expires)})
        require_existing_file(local_path)
        response = FileResponse(local_path)
        response.headers["Cache-Control"] = build_signed_file_cache_control(expires)
        return response
    try:
        response = storage.range_response(
            normalized_path,
            request.headers.get("range"),
            "application/octet-stream",
        )
        response.headers["Cache-Control"] = build_signed_file_cache_control(expires)
        return response
    except StorageNotFound as exc:
        from src.api.exception.errors import ApiError
        raise ApiError(404, "file_not_found", "文件不存在") from exc
