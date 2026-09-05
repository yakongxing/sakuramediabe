from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import FileResponse

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import verify_image_signature
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
        require_existing_file(local_path)
        return FileResponse(local_path)
    try:
        return storage.range_response(normalized_path, request.headers.get("range"), "application/octet-stream")
    except StorageNotFound as exc:
        from src.api.exception.errors import ApiError
        raise ApiError(404, "file_not_found", "文件不存在") from exc
