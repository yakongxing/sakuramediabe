"""Upload protocol tests through the installed webdav4 client and HTTP transport."""

import hashlib
import time
from collections import Counter
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest
from webdav4.client import Client

from src.storage import webdav
from src.storage.types import (
    StorageIntegrityError,
    StoragePublicationUnknown,
    StorageUnavailable,
)


def test_zip_upload_puts_final_key_and_verifies_full_content(dav, tmp_path):
    backend, server, _ = dav
    source = tmp_path / "thumbnails.zip"
    content = b"ZIP bytes" * 10000
    source.write_bytes(content)
    result = backend.put_zip("thumbnails/generation.zip", source, size=len(content), sha256=hashlib.sha256(content).hexdigest())
    assert result.size == len(content)
    assert server.objects["/dav/assets/thumbnails/generation.zip"] == content
    methods = [method for method, _, _ in server.requests]
    assert methods.count("PUT") == methods.count("GET") == 1
    assert not {"MOVE", "DELETE"}.intersection(methods)
    assert not any(".uploading-" in path for _, path, _ in server.requests)


@pytest.mark.parametrize("fault_kind", ["truncated", "same_size_corrupt", "read_failed", "put_failed", "response_lost"])
def test_zip_upload_failure_retains_source_and_does_not_retry_internally(dav, tmp_path, fault_kind):
    backend, server, _ = dav
    source = tmp_path / "thumbnails.zip"
    content = b"complete ZIP content"
    source.write_bytes(content)

    def fault(request):
        if request.method == "PUT":
            if fault_kind == "put_failed":
                return httpx.Response(503)
            if fault_kind == "response_lost":
                server.normal(request)
                raise httpx.ReadTimeout("response lost", request=request)
        if request.method == "GET":
            if fault_kind == "truncated":
                return httpx.Response(200, content=content[:-1])
            if fault_kind == "same_size_corrupt":
                return httpx.Response(200, content=b"x" * len(content))
            if fault_kind == "read_failed":
                return httpx.Response(503)

    server.fault = fault
    with pytest.raises(StorageUnavailable):
        backend.put_zip("thumbnails/generation.zip", source, size=len(content), sha256=hashlib.sha256(content).hexdigest())
    assert source.read_bytes() == content
    assert sum(method == "PUT" for method, _, _ in server.requests) == 1
    assert not any(method in {"MOVE", "DELETE"} for method, _, _ in server.requests)
    server.fault = lambda request: None
    backend.put_zip("thumbnails/generation.zip", source, size=len(content), sha256=hashlib.sha256(content).hexdigest())
    assert server.objects["/dav/assets/thumbnails/generation.zip"] == content


def test_zip_upload_rejects_changed_source(dav, tmp_path):
    backend, server, _ = dav
    source = tmp_path / "thumbnails.zip"
    source.write_bytes(b"changed")
    with pytest.raises(StorageIntegrityError, match="source changed content"):
        backend.put_zip("thumbnails/generation.zip", source, size=7, sha256=hashlib.sha256(b"initial").hexdigest())
    assert source.read_bytes() == b"changed"
    assert not any(method in {"GET", "MOVE", "DELETE"} for method, _, _ in server.requests)


class DAVServer:
    def __init__(self):
        self.directories = {"/dav"}
        self.objects = {}
        self.requests = []
        self.fault = lambda request: None

    def handle(self, request):
        self.requests.append((request.method, request.url.path.rstrip("/"), request.content))
        response = self.fault(request)
        return response if response is not None else self.normal(request)

    def normal(self, request):
        path = request.url.path.rstrip("/")
        parent = path.rsplit("/", 1)[0]
        method = request.method
        if method == "MKCOL":
            if path in self.directories:
                return httpx.Response(405)
            if parent not in self.directories:
                return httpx.Response(409)
            self.directories.add(path)
            return httpx.Response(201)
        if method == "PROPFIND":
            if path not in self.directories and path not in self.objects:
                return httpx.Response(404)
            resource_type = "<d:collection/>" if path in self.directories else ""
            length = len(self.objects.get(path, b""))
            xml = (
                '<d:multistatus xmlns:d="DAV:"><d:response>'
                f'<d:href>{escape(request.url.path)}</d:href><d:propstat><d:prop>'
                f'<d:resourcetype>{resource_type}</d:resourcetype>'
                f'<d:getcontentlength>{length}</d:getcontentlength>'
                '</d:prop><d:status>HTTP/1.1 200 OK</d:status>'
                '</d:propstat></d:response></d:multistatus>'
            )
            return httpx.Response(207, text=xml, headers={"Content-Type": "application/xml"})
        if method == "PUT":
            if parent not in self.directories:
                return httpx.Response(409)
            self.objects[path] = request.content
            return httpx.Response(201)
        if method == "MOVE":
            destination = urlsplit(request.headers["Destination"]).path
            if path not in self.objects:
                return httpx.Response(404)
            if destination.rsplit("/", 1)[0] not in self.directories:
                return httpx.Response(409)
            if destination in self.objects and request.headers["Overwrite"] == "F":
                return httpx.Response(412)
            self.objects[destination] = self.objects.pop(path)
            return httpx.Response(201)
        if method == "GET":
            if path not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, content=self.objects[path])
        if method == "DELETE":
            if path not in self.objects:
                return httpx.Response(404)
            del self.objects[path]
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def dav(monkeypatch):
    server = DAVServer()
    transport = httpx.MockTransport(server.handle)
    monkeypatch.setattr(webdav, "Client", lambda *args, **kwargs: Client(*args, transport=transport, **kwargs))
    sleeps = []
    backend = webdav.WebDAVStorageBackend(
        "https://dav.test/dav", "assets", sleep=sleeps.append,
        retry_delays=(0, 0), final_visibility_retry_delays=(0, 0),
        upload_chunk_size=65536,
    )
    backend.http.close()
    backend.http = httpx.Client(transport=transport)
    backend._last_temp_cleanup_at = time.monotonic()
    yield backend, server, sleeps
    backend.client.http.close()
    backend.http.close()


@pytest.fixture
def unconfirmed_dav(dav):
    backend, server, _ = dav

    def fault(request):
        if request.method == "MOVE":
            server.normal(request)
            return httpx.Response(202)

    server.fault = fault
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    server.fault = lambda request: None
    server.requests.clear()
    return dav


@pytest.fixture
def absent_unconfirmed_dav(dav):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(500) if request.method == "MOVE" else None
    with pytest.raises(StoragePublicationUnknown, match="move outcome unknown"):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert "/dav/assets/a.webp" not in server.objects
    server.fault = lambda request: None
    server.requests.clear()
    return dav


@pytest.mark.parametrize("from_file", [False, True])
def test_unknown_immutable_publication_reuploads_missing_destination(absent_unconfirmed_dav, tmp_path, from_file):
    backend, server, _ = absent_unconfirmed_dav
    old_temps = set(server.objects)
    operation_id = next(iter(backend._uncertain_publications.values()))

    def inspect(request):
        if request.method == "MOVE":
            assert request.headers["Overwrite"] == "F"

    server.fault = inspect
    if from_file:
        source = tmp_path / "image.webp"
        source.write_bytes(b"image")
        result = backend.put_file("a.webp", source, immutable=True, overwrite=False)
    else:
        result = backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)

    assert result.created and result.size == 5 and result.operation_id == operation_id
    assert server.objects["/dav/assets/a.webp"] == b"image"
    assert old_temps.issubset(server.objects)
    assert not backend._uncertain_publications
    assert [method for method, _, _ in server.requests] == ["PROPFIND"] * 3 + ["PUT", "MOVE"]
    assert all(path not in old_temps for method, path, _ in server.requests if method == "PUT")


@pytest.mark.parametrize("overwrite", [False, True])
def test_unknown_nonimmutable_publication_never_reuploads_after_metadata_404(dav, overwrite):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(500) if request.method == "MOVE" else None
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", overwrite=overwrite)
    server.requests.clear()
    server.fault = lambda request: None

    with pytest.raises(StoragePublicationUnknown, match="published object cannot be verified"):
        backend.put_bytes("a.webp", b"image", overwrite=overwrite)
    assert [method for method, _, _ in server.requests] == ["PROPFIND"] * 3
    assert backend._uncertain_publications


@pytest.mark.parametrize("failure", [500, 503, "timeout"])
def test_unknown_metadata_failure_does_not_allow_reupload(absent_unconfirmed_dav, failure):
    backend, server, _ = absent_unconfirmed_dav

    def fault(request):
        if request.method == "PROPFIND":
            if failure == "timeout":
                raise httpx.ReadTimeout("metadata unavailable", request=request)
            return httpx.Response(failure)

    server.fault = fault
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert [method for method, _, _ in server.requests] == ["PROPFIND"] * 3
    assert backend._uncertain_publications


def test_unknown_mixed_metadata_errors_do_not_prove_absence(absent_unconfirmed_dav):
    backend, server, _ = absent_unconfirmed_dav
    reads = 0

    def fault(request):
        nonlocal reads
        if request.method == "PROPFIND":
            reads += 1
            return httpx.Response(503 if reads == 1 else 404)

    server.fault = fault
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert [method for method, _, _ in server.requests] == ["PROPFIND"] * 3
    assert backend._uncertain_publications


@pytest.mark.parametrize("status", [403, 503])
def test_failed_unknown_reupload_keeps_original_publication_uncertainty(absent_unconfirmed_dav, status):
    backend, server, _ = absent_unconfirmed_dav
    previous = dict(backend._uncertain_publications)
    server.fault = lambda request: httpx.Response(status) if request.method == "PUT" else None

    with pytest.raises(StoragePublicationUnknown) as caught:
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert caught.value.publication_possible
    assert caught.value.status_code == status
    assert caught.value.retryable is (status == 503)
    assert backend._uncertain_publications == previous
    assert any(method == "PUT" for method, _, _ in server.requests)
    assert not any(method in {"MOVE", "DELETE"} for method, _, _ in server.requests)
    server.fault = lambda request: None
    assert backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False).size == 5
    assert not backend._uncertain_publications


@pytest.mark.parametrize("content", [b"image", b"other"])
def test_unknown_reupload_preserves_late_move_conflict_protection(absent_unconfirmed_dav, content):
    backend, server, _ = absent_unconfirmed_dav
    old_temps = set(server.objects)

    def late_move(request):
        if request.method == "MOVE":
            assert request.headers["Overwrite"] == "F"
            server.objects["/dav/assets/a.webp"] = content

    server.fault = late_move
    if content == b"image":
        result = backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
        assert result.disposition == "reused" and not result.created
        assert not backend._uncertain_publications
    else:
        with pytest.raises(FileExistsError, match="content mismatch"):
            backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert server.objects["/dav/assets/a.webp"] == content
    assert old_temps.issubset(server.objects)
    assert sum(method == "PUT" for method, _, _ in server.requests) == 1
    assert sum(method == "MOVE" for method, _, _ in server.requests) == 1
    assert sum(method == "GET" for method, _, _ in server.requests) == 1
    assert all(path not in old_temps for method, path, _ in server.requests if method == "DELETE")


def test_directory_cache_and_normal_move_request_counts(dav):
    backend, server, _ = dav
    backend.put_bytes("movies/a/1.webp", b"first")
    first_count = Counter(method for method, _, _ in server.requests)
    backend.put_bytes("movies/a/2.webp", b"second")
    counts = Counter(method for method, _, _ in server.requests)
    assert counts["MKCOL"] == first_count["MKCOL"] == 3
    assert counts["PUT"] == counts["MOVE"] == 2
    assert counts["PROPFIND"] == counts["DELETE"] == counts["GET"] == 0
    assert backend.client.chunk_size == 65536


def test_immutable_success_moves_without_remote_verification(dav):
    backend, server, _ = dav
    backend.put_bytes("movies/hash.webp", b"image", immutable=True, overwrite=False)
    methods = [method for method, _, _ in server.requests]
    assert methods[methods.index("PUT") + 1:] == ["MOVE"]
    assert all(".uploading-" in path for method, path, _ in server.requests if method == "PUT")


@pytest.mark.parametrize("immutable", [False, True])
@pytest.mark.parametrize("from_file", [False, True])
@pytest.mark.parametrize("move_status", [201, 204])
@pytest.mark.parametrize("read_failure", [404, 503, "transport"])
def test_successful_upload_does_not_depend_on_remote_reads(dav, tmp_path, immutable, from_file, move_status, read_failure):
    backend, server, sleeps = dav

    def fault(request):
        if request.method in {"PROPFIND", "GET"}:
            if read_failure == "transport":
                raise httpx.ReadError("remote reads unavailable", request=request)
            return httpx.Response(read_failure)
        if request.method == "MOVE":
            assert request.headers["Overwrite"] == ("F" if immutable else "T")
            server.normal(request)
            return httpx.Response(move_status)

    server.fault = fault
    if from_file:
        source = tmp_path / "image.webp"
        source.write_bytes(b"image")
        result = backend.put_file("a.webp", source, immutable=immutable, overwrite=not immutable)
    else:
        result = backend.put_bytes("a.webp", b"image", immutable=immutable, overwrite=not immutable)

    assert result.key == "a.webp" and result.size == 5 and result.is_file
    assert result.etag is None and result.operation_id
    assert result.disposition == ("created" if move_status == 201 else "published")
    assert server.objects == {"/dav/assets/a.webp": b"image"}
    assert [method for method, _, _ in server.requests] == ["MKCOL", "PUT", "MOVE"]
    assert sleeps == []


@pytest.mark.parametrize("immutable", [False, True])
def test_put_timeout_rewinds_into_an_isolated_attempt_key(dav, immutable):
    backend, server, sleeps = dav
    attempted = []

    def fault(request):
        if request.method == "PUT":
            attempted.append((request.url.path, request.content))
            if len(attempted) == 1:
                raise httpx.ReadTimeout("lost response", request=request)

    server.fault = fault
    assert backend.put_bytes("movies/a.webp", b"complete image", immutable=immutable, overwrite=not immutable).size == 14
    assert attempted[0][0] != attempted[1][0]
    assert attempted[0][1] == attempted[1][1] == b"complete image"
    assert len(attempted) == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("status", [401, 403, 507])
@pytest.mark.parametrize("immutable", [False, True])
def test_permanent_put_error_does_not_retry_or_download(dav, status, immutable):
    backend, server, sleeps = dav
    server.fault = lambda request: httpx.Response(status) if request.method == "PUT" else None
    with pytest.raises(StorageUnavailable) as caught:
        backend.put_bytes("a.webp", b"image", immutable=immutable, overwrite=not immutable)
    assert not isinstance(caught.value, StoragePublicationUnknown)
    assert caught.value.status_code == status
    assert not caught.value.retryable
    assert sum(method == "PUT" for method, _, _ in server.requests) == 1
    assert not any(method == "GET" for method, _, _ in server.requests)
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_is_bounded_without_nested_retries(dav, status):
    backend, server, sleeps = dav
    server.fault = lambda request: httpx.Response(status, headers={"Retry-After": "120"}) if request.method == "PUT" else None
    with pytest.raises(StorageUnavailable):
        backend.put_bytes("a.webp", b"image")
    assert sleeps == [60, 60]
    assert sum(method == "PUT" for method, _, _ in server.requests) == 3


def test_missing_cached_parent_is_recreated_once(dav):
    backend, server, _ = dav
    backend.put_bytes("movies/a.webp", b"image")
    server.directories.remove("/dav/assets/movies")
    assert backend.put_bytes("movies/b.webp", b"image").size == 5
    assert "/dav/assets/movies" in server.directories
    put_paths = [path for method, path, _ in server.requests if method == "PUT"]
    assert len(put_paths) == 3
    assert put_paths[-1] != put_paths[-2]


def test_mkdir_409_is_not_accepted_as_success(dav):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(409) if request.method == "MKCOL" else None
    with pytest.raises(StorageUnavailable) as caught:
        backend.put_bytes("movies/a.webp", b"image")
    assert caught.value.stage == "mkdir"
    assert sum(method == "MKCOL" for method, _, _ in server.requests) == 2
    assert not any(method == "PUT" for method, _, _ in server.requests)


@pytest.mark.parametrize("immutable", [False, True])
def test_unconfirmed_invisible_result_is_unknown_and_retry_reconciles(dav, immutable):
    backend, server, _ = dav
    final_path = "/dav/assets/a.webp"

    def invisible(request):
        if request.method == "MOVE":
            server.normal(request)
            raise httpx.ReadTimeout("response lost", request=request)
        if request.method == "PROPFIND" and request.url.path == final_path and final_path in server.objects:
            return httpx.Response(404)

    server.fault = invisible
    with pytest.raises(StoragePublicationUnknown) as caught:
        backend.put_bytes("a.webp", b"image", immutable=immutable, overwrite=not immutable)
    assert caught.value.key == "a.webp"
    assert server.objects[final_path] == b"image"
    puts = sum(method == "PUT" for method, _, _ in server.requests)
    server.fault = lambda request: None
    assert backend.put_bytes("a.webp", b"image", immutable=immutable, overwrite=not immutable).size == 5
    assert sum(method == "PUT" for method, _, _ in server.requests) == puts


def test_move_response_lost_is_confirmed_by_hash(dav):
    backend, server, _ = dav

    def fault(request):
        if request.method == "MOVE":
            server.normal(request)
            raise httpx.ReadTimeout("response lost", request=request)

    server.fault = fault
    assert backend.put_bytes("a.webp", b"image").size == 5
    assert sum(method == "MOVE" for method, _, _ in server.requests) == 1
    assert sum(method == "GET" for method, _, _ in server.requests) == 1


@pytest.mark.parametrize("status", [404, 503])
def test_unconfirmed_publication_download_failure_is_not_content_mismatch(unconfirmed_dav, status):
    backend, server, _ = unconfirmed_dav
    server.fault = lambda request: httpx.Response(status) if request.method == "GET" else None
    with pytest.raises(StoragePublicationUnknown) as caught:
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert caught.value.stage == "verify"
    assert sum(method == "GET" for method, _, _ in server.requests) == 3
    assert server.objects == {"/dav/assets/a.webp": b"image"}
    assert not any(method in {"PUT", "MOVE", "DELETE"} for method, _, _ in server.requests)


def test_unconfirmed_publication_rejects_equal_size_wrong_content(unconfirmed_dav):
    backend, server, _ = unconfirmed_dav
    server.fault = lambda request: httpx.Response(200, content=b"wrong") if request.method == "GET" else None
    with pytest.raises(StorageUnavailable, match="content mismatch") as caught:
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert not isinstance(caught.value, StoragePublicationUnknown)
    assert caught.value.publication_possible
    assert not any(method in {"PUT", "MOVE", "DELETE"} for method, _, _ in server.requests)


def test_get_404_after_stat_is_retried_during_reconciliation(unconfirmed_dav):
    backend, server, _ = unconfirmed_dav
    gets = []

    def fault(request):
        if request.method == "GET":
            gets.append(request)
            if len(gets) == 1:
                return httpx.Response(404)

    server.fault = fault
    assert backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False).size == 5
    assert len(gets) == 2


def test_directory_cache_expires(dav):
    backend, server, _ = dav
    backend.put_bytes("a.webp", b"image")
    backend._directory_cache["assets"] = time.monotonic() - 1
    backend.put_bytes("b.webp", b"image")
    assert sum(method == "MKCOL" for method, _, _ in server.requests) == 2


def test_non_transport_exception_is_not_retried(dav):
    backend, server, sleeps = dav

    def fault(request):
        if request.method == "PUT":
            raise ValueError("programming error")

    server.fault = fault
    with pytest.raises(StorageUnavailable) as caught:
        backend.put_bytes("a.webp", b"image")
    assert not caught.value.retryable
    assert sleeps == []


def test_digest_comparison_distinguishes_missing_from_mismatch(dav):
    from src.storage.types import StorageNotFound

    backend, server, _ = dav
    with pytest.raises(StorageNotFound):
        backend._destination_matches("missing.webp", expected_size=5, expected_sha256=hashlib.sha256(b"image").hexdigest())
    server.objects["/dav/assets/a.webp"] = b"wrong"
    assert backend._destination_matches("a.webp", expected_size=5, expected_sha256=hashlib.sha256(b"image").hexdigest()) is None


def test_broken_reconciliation_stream_has_bounded_retries_and_is_closed(unconfirmed_dav):
    backend, server, sleeps = unconfirmed_dav
    streams = []

    class BrokenStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"im"
            raise httpx.ReadError("connection lost")

        def close(self):
            self.closed = True

    def fault(request):
        if request.method == "GET":
            stream = BrokenStream()
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"Accept-Ranges": "bytes"})

    server.fault = fault
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert len(streams) == 3
    assert all(stream.closed for stream in streams)
    assert len(sleeps) == 2
    assert not any(method in {"PUT", "MOVE", "DELETE"} for method, _, _ in server.requests)


def test_conflict_with_unreadable_destination_is_unknown(dav):
    backend, server, _ = dav
    server.objects["/dav/assets/a.webp"] = b"image"
    server.fault = lambda request: httpx.Response(503) if request.method == "GET" and request.url.path == "/dav/assets/a.webp" else None
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert all(path != "/dav/assets/a.webp" for method, path, _ in server.requests if method == "PUT")
    assert server.objects["/dav/assets/a.webp"] == b"image"


def test_existing_directory_path_must_be_a_collection(dav):
    backend, server, _ = dav
    server.objects["/dav/assets"] = b"file"
    server.fault = lambda request: httpx.Response(405) if request.method == "MKCOL" else None
    with pytest.raises(StorageUnavailable, match="not a collection"):
        backend.put_bytes("a.webp", b"image")
    assert not backend._directory_cache


def test_directory_cache_is_bounded(dav):
    backend, _, _ = dav
    backend._directory_cache.update((f"old-{index}", time.monotonic() + 600) for index in range(4096))
    backend.put_bytes("a.webp", b"image")
    assert len(backend._directory_cache) == 4096
    assert "old-0" not in backend._directory_cache


@pytest.mark.parametrize("method", ["MKCOL", "PROPFIND", "DELETE"])
def test_metadata_retries_are_not_multiplied_by_webdav4(dav, method):
    backend, server, sleeps = dav
    server.fault = lambda request: httpx.Response(503) if request.method == method else None
    with pytest.raises(StorageUnavailable):
        if method == "MKCOL":
            backend.put_bytes("a.webp", b"image")
        elif method == "PROPFIND":
            backend.stat("a.webp")
        else:
            backend.delete("a.webp")
    assert sum(verb == method for verb, _, _ in server.requests) == 3
    assert len(sleeps) == 2


def test_immutable_publish_is_atomic_create_and_returns_receipt(dav):
    backend, server, _ = dav
    result = backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert result.created
    assert result.operation_id
    assert any(method == "MOVE" for method, _, _ in server.requests)
    assert all(path != "/dav/assets/a.webp" for method, path, _ in server.requests if method == "PUT")


def test_immutable_existing_content_is_reused_without_ownership(dav):
    backend, server, _ = dav
    backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    result = backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert not result.created
    assert result.disposition == "reused"
    assert server.objects == {"/dav/assets/a.webp": b"image"}


def test_strict_create_never_reuses_existing_identical_content(dav):
    backend, server, _ = dav
    backend.put_bytes("a.webp", b"image")
    with pytest.raises(FileExistsError):
        backend.put_bytes("a.webp", b"image", overwrite=False)
    assert server.objects == {"/dav/assets/a.webp": b"image"}


def test_upload_backoff_releases_network_slot(dav):
    backend, server, _ = dav
    import threading

    backend._publication_semaphore = threading.BoundedSemaphore(1)
    waits = []

    def sleep(delay):
        assert backend._publication_semaphore.acquire(blocking=False)
        backend._publication_semaphore.release()
        waits.append(delay)

    backend._sleep = sleep
    server.fault = lambda request: httpx.Response(503) if request.method == "PUT" else None
    with pytest.raises(StorageUnavailable):
        backend.put_bytes("a.webp", b"image")
    assert waits


@pytest.mark.parametrize("status", [200, 202, 207])
def test_unexpected_move_response_preserves_possible_source_and_final(dav, status):
    backend, server, _ = dav
    server.objects["/dav/assets/a.webp"] = b"older"

    def fault(request):
        if request.method == "MOVE":
            if status == 207:
                return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/assets/a.webp</d:href><d:status>HTTP/1.1 500 Internal Server Error</d:status></d:response></d:multistatus>')
            return httpx.Response(status)

    server.fault = fault
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image")
    assert server.objects["/dav/assets/a.webp"] == b"older"
    assert any(".uploading-" in key for key in server.objects)
    assert not any(method == "DELETE" for method, _, _ in server.requests)


@pytest.mark.parametrize("content", [b"image", b"other"])
def test_competing_create_during_move_is_never_overwritten(dav, content):
    backend, server, _ = dav

    def fault(request):
        if request.method == "MOVE":
            assert request.headers["Overwrite"] == "F"
            server.objects["/dav/assets/a.webp"] = content

    server.fault = fault
    with pytest.raises(FileExistsError):
        backend.put_bytes("a.webp", b"image", overwrite=False)
    assert server.objects == {"/dav/assets/a.webp": content}


def test_late_timed_out_put_cannot_change_published_bytes(dav):
    backend, server, _ = dav
    stalled = []

    def fault(request):
        if request.method == "PUT" and not stalled:
            stalled.append(request.url.path)
            raise httpx.ReadTimeout("still executing on server", request=request)

    server.fault = fault
    backend.put_bytes("a.webp", b"image", overwrite=False)
    server.objects[stalled[0]] = b"late partial bytes"
    assert server.objects["/dav/assets/a.webp"] == b"image"
    assert not any(method == "DELETE" and path == stalled[0] for method, path, _ in server.requests)


def test_move_budget_exhaustion_keeps_unknown_classification(dav):
    backend, server, _ = dav

    def fault(request):
        if request.method == "MOVE":
            server.normal(request)
            raise httpx.ReadTimeout("lost response", request=request)
        if request.method == "PROPFIND" and request.url.path == "/dav/assets/a.webp":
            return httpx.Response(404)

    def expire(_delay):
        webdav._budget.get().deadline = time.monotonic() - 1

    backend._sleep = expire
    server.fault = fault
    with pytest.raises(StoragePublicationUnknown) as caught:
        backend.put_bytes("a.webp", b"image")
    assert caught.value.publication_possible
    assert server.objects["/dav/assets/a.webp"] == b"image"
    assert not any(method == "DELETE" for method, _, _ in server.requests)


def test_slow_small_reconciliation_chunks_check_deadline_and_close(unconfirmed_dav):
    backend, server, _ = unconfirmed_dav

    class SlowStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"i"
            webdav._budget.get().deadline = time.monotonic() - 1
            yield b"m"
            pytest.fail("verification read past its deadline")

        def close(self):
            self.closed = True

    stream = SlowStream()
    server.fault = lambda request: httpx.Response(200, stream=stream) if request.method == "GET" else None
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", overwrite=False, immutable=True)
    assert stream.closed
    assert not any(method in {"PUT", "MOVE", "DELETE"} for method, _, _ in server.requests)


def test_modified_source_is_rejected_before_publication(dav, monkeypatch, tmp_path):
    backend, server, _ = dav
    source = tmp_path / "image.webp"
    source.write_bytes(b"image")
    ensure_parents = backend._ensure_parents

    def change_source(key, repaired):
        ensure_parents(key, repaired)
        source.write_bytes(b"other content")

    monkeypatch.setattr(backend, "_ensure_parents", change_source)
    with pytest.raises(StorageUnavailable, match="source changed"):
        backend.put_file("a.webp", source)
    assert not any(method in {"PUT", "MOVE"} for method, _, _ in server.requests)


def test_source_changed_during_upload_is_rejected_before_move(dav, tmp_path):
    backend, server, _ = dav
    source = tmp_path / "image.webp"
    source.write_bytes(b"image")

    def fault(request):
        if request.method == "PUT":
            server.normal(request)
            source.write_bytes(b"changed content")
            return httpx.Response(201)

    server.fault = fault
    with pytest.raises(StorageUnavailable, match="source changed during publication"):
        backend.put_file("a.webp", source, immutable=True, overwrite=False)
    assert not any(method == "MOVE" for method, _, _ in server.requests)
    assert any(method == "DELETE" for method, _, _ in server.requests)
    assert not server.objects


def test_zero_byte_upload_is_not_missing_length(dav):
    backend, server, _ = dav
    result = backend.put_bytes("empty.srt", b"", overwrite=False, immutable=True)
    assert result.size == 0 and result.created
    assert server.objects["/dav/assets/empty.srt"] == b""


def test_metadata_confirmation_uses_depth_zero(dav):
    backend, server, _ = dav

    def inspect(request):
        if request.method == "PROPFIND":
            assert request.headers["Depth"] == "0"

    server.fault = inspect
    backend.put_bytes("a.webp", b"image")
    backend._directory_cache.clear()
    backend.put_bytes("b.webp", b"image")


def test_cleanup_requires_explicit_writer_pause(dav):
    from datetime import datetime, timezone

    backend, server, _ = dav
    with pytest.raises(ValueError, match="pause"):
        backend.cleanup_expired_uploads("movies", older_than=datetime.now(timezone.utc))
    assert not server.requests


@pytest.mark.parametrize("unknown", [False, True])
def test_thumbnail_batch_resumes_with_real_webdav_client(dav, test_db, tmp_path, monkeypatch, unknown):
    from PIL import Image as PILImage

    from src.config import settings
    from src.model import Media, MediaLibrary, MediaThumbnail, Movie
    from src.plugins.provider_protocol import ThumbnailArtifact
    from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
    from src.service.playback.thumbnails.batches import ThumbnailBatchStore

    backend, server, _ = dav
    monkeypatch.setattr("src.service.playback.thumbnails.artifacts.asset_storage", lambda: backend)
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.storage, "webdav_base_url", "https://dav.test/dav")
    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 1)
    movie = Movie.create(movie_number="DAV-001", javdb_id="dav-001", title="movie")
    library = MediaLibrary.create(name="dav", provider_key="fake", provider_config={})
    media = Media.create(movie=movie, library=library, file_name="video.mp4")
    source = tmp_path / "source.webp"
    PILImage.new("RGB", (32, 18)).save(source, "WEBP")
    artifacts = [(ThumbnailArtifact(offset, "source.webp"), source) for offset in (3, 6)]

    def fault(request):
        if unknown and request.method == "PUT" and request.url.path.endswith(".zip"):
            server.normal(request)
            raise httpx.ReadTimeout("response lost", request=request)
        if unknown and request.method == "PROPFIND" and request.url.path.endswith(".zip"):
            return httpx.Response(404)
        if not unknown and request.method == "PUT" and request.url.path.endswith(".zip"):
            return httpx.Response(503)

    server.fault = fault
    with pytest.raises(StorageUnavailable):
        ThumbnailArtifactService.persist(media, artifacts)
    batch = ThumbnailBatchStore(media).load()
    assert batch is not None
    saved_zip = batch.pack_file.read_bytes()
    finals = {path for path in server.objects if path.endswith(".zip")}
    assert len(finals) == (1 if unknown else 0)
    assert not MediaThumbnail.select().exists()
    boundary = len(server.requests)
    server.fault = lambda request: None
    backend._uncertain_publications.clear()
    monkeypatch.setattr("src.service.playback.thumbnails.batches.write_pack", lambda *_: pytest.fail("rebuilt saved ZIP"))
    assert ThumbnailArtifactService.persist(media, []) == 2
    resumed = server.requests[boundary:]
    assert sum(method == "PUT" for method, _, _ in resumed) == 1
    assert not any(method == "MOVE" for method, _, _ in server.requests)
    assert not any(method == "DELETE" and path.endswith(".zip") for method, path, _ in server.requests)
    assert finals.issubset(server.objects)
    assert next(content for path, content in server.objects.items() if path.endswith(".zip")) == saved_zip
    assert not batch.workspace.exists()
    assert MediaThumbnail.select().count() == 2
