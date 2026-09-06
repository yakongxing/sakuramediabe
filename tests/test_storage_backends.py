import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from src.storage.keys import normalize_storage_key
from src.storage.local import LocalStorageBackend


@pytest.mark.parametrize("key", ["", "/absolute", "a//b", "a/../b", "a\\b", "a/%2e%2e/b", "a/%252e%252e/b", "a\x00b"])
def test_storage_key_rejects_unsafe_values(key):
    with pytest.raises(ValueError):
        normalize_storage_key(key)


def test_storage_key_preserves_safe_posix_key():
    assert normalize_storage_key("movies/ab/ABP-001/cover.jpg") == "movies/ab/ABP-001/cover.jpg"


def test_local_backend_round_trip(tmp_path):
    backend = LocalStorageBackend(tmp_path / "assets")
    backend.put_bytes("movies/a.jpg", b"image")
    assert backend.stat("movies/a.jpg").size == 5
    with backend.open("movies/a.jpg") as handle:
        assert handle.read() == b"image"
    backend.delete("movies/a.jpg")
    assert not backend.exists("movies/a.jpg")


def test_local_backend_failed_copy_never_truncates_existing_target(monkeypatch, tmp_path):
    backend = LocalStorageBackend(tmp_path / "assets")
    backend.put_bytes("movies/a.jpg", b"old")
    source = tmp_path / "new.jpg"
    source.write_bytes(b"new")
    monkeypatch.setattr("src.storage.local.shutil.copyfile", lambda *args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        backend.put_file("movies/a.jpg", source)

    with backend.open("movies/a.jpg") as handle:
        assert handle.read() == b"old"


def test_webdav_backend_maps_namespace(monkeypatch):
    from src.storage import webdav as module

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def info(self, path):
            assert path == "tenant/assets/movies/a.jpg"
            return {"size": 7, "type": "file", "etag": "x"}

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", root_prefix="tenant")
    assert backend.stat("movies/a.jpg").size == 7


def test_webdav_put_file_publishes_through_temporary_key(monkeypatch, tmp_path):
    from src.storage import webdav as module

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass

        def info(self, path):
            if path.startswith("tenant/assets/movies/.cover.jpg.uploading-"):
                return {"size": 5, "type": "file", "etag": "tmp"}
            if path == "tenant/assets/movies/cover.jpg":
                return {"size": 5, "type": "file", "etag": "final"}
            raise module.ResourceNotFound(path)

        def mkdir(self, path):
            calls.append(("mkdir", path))
            raise module.ResourceAlreadyExists(path)

        def upload_fileobj(self, file_obj, to_path, *, overwrite=False, size=None, **kwargs):
            calls.append(("upload", to_path, overwrite, size, file_obj.read()))

        def remove(self, path):
            calls.append(("remove", path))

        def move(self, src_path, dst_path, *, overwrite=False):
            calls.append(("move", src_path, dst_path, overwrite))

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", root_prefix="tenant")
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"image")

    stat = backend.put_file("movies/cover.jpg", source)

    assert stat.size == 5
    uploads = [call for call in calls if call[0] == "upload"]
    assert len(uploads) == 1
    assert uploads[0][1].startswith("tenant/assets/movies/.cover.jpg.uploading-")
    assert uploads[0][2] is True
    assert uploads[0][3] == 5
    assert uploads[0][4] == b"image"
    assert ("remove", "tenant/assets/movies/cover.jpg") not in calls
    moves = [call for call in calls if call[0] == "move"]
    assert moves == [("move", uploads[0][1], "tenant/assets/movies/cover.jpg", True)]
    assert all(call[1] != "tenant/assets/movies/cover.jpg" for call in uploads)


def test_webdav_immutable_put_uploads_directly_to_final_key(monkeypatch, tmp_path):
    from src.storage import webdav as module

    uploads = []
    moves = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def info(self, path):
            assert uploads, "immutable publication must not stat the new final key before PUT"
            return {"size": 5, "type": "file"}
        def upload_fileobj(self, stream, path, overwrite, size):
            uploads.append((path, overwrite, size, stream.read()))
        def move(self, *args, **kwargs): moves.append((args, kwargs))
        def download_fileobj(self, path, target): target.write(b"image")
        def ls(self, *args, **kwargs): return []

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"image")
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets")

    result = backend.put_file(
        "movies/a/cover-deadbeef.jpg", source, overwrite=False, immutable=True
    )

    assert result.size == 5
    assert uploads == [("assets/movies/a/cover-deadbeef.jpg", False, 5, b"image")]
    assert moves == []


def test_webdav_immutable_put_rejects_same_size_wrong_content(monkeypatch, tmp_path):
    from src.storage import webdav as module

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, *args, **kwargs): pass
        def info(self, path): return {"size": 5, "type": "file", "etag": "not-a-hash"}
        def download_fileobj(self, path, target): target.write(b"wrong")

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image")

    with pytest.raises(module.StorageUnavailable, match="content mismatch"):
        module.WebDAVStorageBackend("https://dav.example", "assets").put_file(
            "movies/a/hash.jpg", source, overwrite=False, immutable=True
        )


def test_webdav_put_retries_eventual_temp_visibility_404(monkeypatch):
    from src.storage import webdav as module

    info_calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path):
            info_calls.append(path)
            if ".uploading-" in path and info_calls.count(path) == 1:
                raise module.ResourceNotFound(path)
            return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): pass
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    sleeps = []
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=sleeps.append,
    )

    assert backend.put_bytes("movies/cover.jpg", b"image").size == 5
    assert sleeps


def test_webdav_put_retries_transient_move_423(monkeypatch):
    from src.storage import webdav as module

    class DavError(Exception):
        def __init__(self, status):
            self.response = type("Response", (), {"status_code": status})()

    moves = []
    objects = set()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): objects.add(to_path)
        def info(self, path):
            if path not in objects:
                raise module.ResourceNotFound(path)
            return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False):
            moves.append((src_path, dst_path, overwrite))
            if len(moves) == 1:
                raise DavError(423)
            objects.remove(src_path)
            objects.add(dst_path)
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    backend.put_bytes("movies/cover.jpg", b"image")

    assert len(moves) == 2
    assert all(move[2] is True for move in moves)


def test_webdav_put_accepts_ambiguous_move_that_already_published(monkeypatch):
    from src.storage import webdav as module

    objects = {}
    move_calls = 0

    class DavError(Exception):
        def __init__(self):
            self.response = type("Response", (), {"status_code": 503})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs):
            objects[to_path] = file_obj.read()
        def info(self, path):
            if path not in objects:
                raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def download_fileobj(self, path, target): target.write(objects[path])
        def move(self, src_path, dst_path, *, overwrite=False):
            nonlocal move_calls
            move_calls += 1
            objects[dst_path] = objects.pop(src_path)
            raise DavError()
        def remove(self, path):
            if path not in objects:
                raise module.ResourceNotFound(path)
            del objects[path]

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    result = backend.put_bytes("movies/cover.jpg", b"image")

    assert result.size == 5
    assert move_calls == 1
    assert objects == {"assets/movies/cover.jpg": b"image"}


def test_webdav_ambiguous_move_does_not_accept_old_equal_size_destination(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StorageUnavailable

    objects = {"assets/movies/cover.jpg": b"older"}
    moves = 0

    class DavError(Exception):
        def __init__(self):
            self.response = type("Response", (), {"status_code": 503})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): objects[to_path] = file_obj.read()
        def info(self, path):
            if path not in objects: raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def download_fileobj(self, path, target): target.write(objects[path])
        def move(self, src_path, dst_path, *, overwrite=False):
            nonlocal moves
            moves += 1
            raise DavError()
        def remove(self, path): objects.pop(path, None)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None, retry_delays=(0, 0),
    )

    with pytest.raises(StorageUnavailable, match="move"):
        backend.put_bytes("movies/cover.jpg", b"newer")

    assert moves == 3
    assert objects["assets/movies/cover.jpg"] == b"older"


def test_webdav_concurrent_publish_reconciles_412_when_destination_matches(monkeypatch):
    from src.storage import webdav as module

    objects = {}
    move_calls = 0

    class DavError(Exception):
        def __init__(self):
            self.response = type("Response", (), {"status_code": 412})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs):
            objects[to_path] = file_obj.read()
        def info(self, path):
            if path not in objects: raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def download_fileobj(self, path, target): target.write(objects[path])
        def move(self, src_path, dst_path, *, overwrite=False):
            nonlocal move_calls
            move_calls += 1
            if move_calls == 2:
                raise DavError()
            objects[dst_path] = objects.pop(src_path)
        def remove(self, path): objects.pop(path, None)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    first = module.WebDAVStorageBackend("https://dav.example/root", "assets")
    second = module.WebDAVStorageBackend("https://dav.example/root", "assets")

    first.put_bytes("movies/cover.jpg", b"image")
    result = second.put_bytes("movies/cover.jpg", b"image")

    assert result.size == 5
    assert move_calls == 2
    assert objects == {"assets/movies/cover.jpg": b"image"}


def test_webdav_concurrent_publish_rejects_412_when_destination_hash_mismatches(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StorageUnavailable

    objects = {}
    move_calls = 0

    class DavError(Exception):
        def __init__(self):
            self.response = type("Response", (), {"status_code": 412})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs):
            objects[to_path] = file_obj.read()
        def info(self, path):
            if path not in objects: raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def download_fileobj(self, path, target): target.write(objects[path])
        def move(self, src_path, dst_path, *, overwrite=False):
            nonlocal move_calls
            move_calls += 1
            if move_calls > 1:
                raise DavError()
            objects[dst_path] = objects.pop(src_path)
        def remove(self, path): objects.pop(path, None)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    first = module.WebDAVStorageBackend("https://dav.example/root", "assets")
    second = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", retry_delays=()
    )

    first.put_bytes("movies/cover.jpg", b"image")
    with pytest.raises(StorageUnavailable, match=r"move failed \(412\)"):
        second.put_bytes("movies/cover.jpg", b"other")

    assert move_calls == 2
    assert objects == {"assets/movies/cover.jpg": b"image"}


def test_webdav_backend_enforces_publication_concurrency_limit(monkeypatch):
    from src.storage import webdav as module

    active = 0
    maximum_active = 0
    lock = threading.Lock()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
        def info(self, path): return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): pass
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", publication_concurrency_limit=2
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: backend.put_bytes(f"{index}.jpg", b"image"), range(8)))

    assert maximum_active == 2


def test_webdav_put_does_not_retry_permanent_move_error(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StorageUnavailable

    class DavError(Exception):
        def __init__(self):
            self.response = type("Response", (), {"status_code": 401})()

    moves = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path): return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False):
            moves.append((src_path, dst_path))
            raise DavError()
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    with pytest.raises(StorageUnavailable, match=r"move failed \(401\)"):
        backend.put_bytes("movies/cover.jpg", b"image")
    assert len(moves) == 1


def test_webdav_cleanup_failure_does_not_mask_publish_error(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StorageUnavailable

    class DavError(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.response = type("Response", (), {"status_code": status})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path): return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False):
            raise DavError(401, "publish denied")
        def remove(self, path):
            raise DavError(500, "cleanup down")

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    with pytest.raises(StorageUnavailable, match=r"move failed \(401\)"):
        backend.put_bytes("movies/cover.jpg", b"image")


def test_webdav_put_retries_eventual_final_visibility_404(monkeypatch):
    from src.storage import webdav as module

    final_info_calls = 0

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path):
            nonlocal final_info_calls
            if ".uploading-" not in path:
                final_info_calls += 1
                if final_info_calls == 1:
                    raise module.ResourceNotFound(path)
            return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): pass
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    assert backend.put_bytes("movies/cover.jpg", b"image").size == 5
    assert final_info_calls == 2


def test_webdav_final_visibility_uses_separate_realistic_retry_window(monkeypatch):
    from src.storage import webdav as module

    final_info_calls = 0
    objects = {}

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): objects[to_path] = file_obj.read()
        def info(self, path):
            nonlocal final_info_calls
            if ".uploading-" in path:
                return {"size": len(objects[path]), "type": "file"}
            final_info_calls += 1
            if final_info_calls <= 5: raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): objects[dst_path] = objects.pop(src_path)
        def remove(self, path): objects.pop(path, None)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
        retry_delays=(0,), final_visibility_retry_delays=(0, 0, 0, 0, 0),
    )

    assert backend.put_bytes("movies/cover.jpg", b"image").size == 5
    assert final_info_calls == 6


def test_webdav_exhausted_final_visibility_reports_possible_publication(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StoragePublicationUnknown

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path):
            if ".uploading-" in path: return {"size": 5, "type": "file"}
            raise module.ResourceNotFound(path)
        def move(self, src_path, dst_path, *, overwrite=False): pass
        def remove(self, path): pass

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
        retry_delays=(), final_visibility_retry_delays=(0, 0),
    )

    with pytest.raises(StoragePublicationUnknown) as raised:
        backend.put_bytes("movies/cover.jpg", b"image")
    assert raised.value.key == "movies/cover.jpg"
    assert raised.value.publication_possible is True


def test_webdav_success_opportunistically_wires_bounded_temp_cleanup(monkeypatch):
    from src.storage import webdav as module

    calls = []
    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): pass
        def info(self, path): return {"size": 5, "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): pass
        def remove(self, path): pass
        def ls(self, path, detail=True): calls.append(path); return []

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
        temp_cleanup_interval_seconds=60,
    )
    backend.put_bytes("movies/a/cover.jpg", b"image")
    backend.put_bytes("movies/a/plot.jpg", b"image")
    assert calls == ["assets/movies/a"]


def test_webdav_successful_publish_leaves_no_temporary_object(monkeypatch):
    from src.storage import webdav as module

    objects = {"assets/movies/cover.jpg": b"old"}

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs):
            objects[to_path] = file_obj.read()
        def info(self, path):
            if path not in objects:
                raise module.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False):
            objects[dst_path] = objects.pop(src_path)
        def remove(self, path):
            if path not in objects:
                raise module.ResourceNotFound(path)
            del objects[path]

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend(
        "https://dav.example/root", "assets", sleep=lambda _: None,
    )

    backend.put_bytes("movies/cover.jpg", b"image")

    assert objects == {"assets/movies/cover.jpg": b"image"}


def test_webdav_cleanup_removes_only_expired_upload_temps(monkeypatch):
    from src.storage import webdav as module

    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    removed = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def ls(self, path, detail=True):
            return [
                {"name": f"{path}/.old.jpg.uploading-{'a' * 32}", "type": "file", "modified": now - timedelta(hours=2)},
                {"name": f"{path}/.new.jpg.uploading-{'b' * 32}", "type": "file", "modified": now - timedelta(minutes=5)},
                {"name": f"{path}/cover.jpg", "type": "file", "modified": now - timedelta(days=2)},
                {"name": f"{path}/.bad.uploading-not-a-uuid", "type": "file", "modified": now - timedelta(days=2)},
            ]
        def remove(self, path): removed.append(path)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", sleep=lambda _: None)

    deleted = backend.cleanup_expired_uploads(
        "movies/ab/ABC-001", older_than=now - timedelta(hours=1)
    )

    assert deleted == [f"movies/ab/ABC-001/.old.jpg.uploading-{'a' * 32}"]
    assert removed == [f"assets/movies/ab/ABC-001/.old.jpg.uploading-{'a' * 32}"]


def test_webdav_failed_publish_preserves_existing_final(monkeypatch):
    from src.storage import webdav as module
    from src.storage.types import StorageUnavailable

    objects = {"assets/movies/cover.jpg": b"old"}

    class DavError(Exception):
        def __init__(self): self.response = type("Response", (), {"status_code": 401})()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): raise module.ResourceAlreadyExists(path)
        def upload_fileobj(self, file_obj, to_path, **kwargs): objects[to_path] = file_obj.read()
        def info(self, path): return {"size": len(objects[path]), "type": "file"}
        def move(self, src_path, dst_path, *, overwrite=False): raise DavError()
        def remove(self, path): objects.pop(path, None)

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", sleep=lambda _: None)

    with pytest.raises(StorageUnavailable):
        backend.put_bytes("movies/cover.jpg", b"new")

    assert objects == {"assets/movies/cover.jpg": b"old"}
