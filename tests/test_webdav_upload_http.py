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
from src.storage.types import StoragePublicationUnknown, StorageUnavailable


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


def test_directory_cache_and_normal_move_request_counts(dav):
    backend, server, _ = dav
    backend.put_bytes("movies/a/1.webp", b"first")
    first_count = Counter(method for method, _, _ in server.requests)
    backend.put_bytes("movies/a/2.webp", b"second")
    counts = Counter(method for method, _, _ in server.requests)
    assert counts["MKCOL"] == first_count["MKCOL"] == 3
    assert counts["PUT"] == counts["MOVE"] == 2
    assert counts["PROPFIND"] == 4
    assert counts["DELETE"] == counts["GET"] == 0
    assert backend.client.chunk_size == 65536


def test_immutable_success_uses_one_final_stat_and_full_get(dav):
    backend, server, _ = dav
    backend.put_bytes("movies/hash.webp", b"image", immutable=True, overwrite=False)
    methods = [method for method, _, _ in server.requests]
    # webdav4 checks existence before PUT; the only post-PUT metadata query is ours.
    assert methods[methods.index("PUT") + 1:] == ["PROPFIND", "GET"]
    assert "MOVE" not in methods


@pytest.mark.parametrize("immutable", [False, True])
def test_put_timeout_rewinds_and_reuses_key(dav, immutable):
    backend, server, sleeps = dav
    attempted = []

    def fault(request):
        if request.method == "PUT":
            attempted.append((request.url.path, request.content))
            if len(attempted) == 1:
                raise httpx.ReadTimeout("lost response", request=request)

    server.fault = fault
    assert backend.put_bytes("movies/a.webp", b"complete image", immutable=immutable, overwrite=not immutable).size == 14
    assert attempted[0] == attempted[1]
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
    assert put_paths[-1] == put_paths[-2]


def test_mkdir_409_is_not_accepted_as_success(dav):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(409) if request.method == "MKCOL" else None
    with pytest.raises(StorageUnavailable) as caught:
        backend.put_bytes("movies/a.webp", b"image")
    assert caught.value.stage == "mkdir"
    assert sum(method == "MKCOL" for method, _, _ in server.requests) == 2
    assert not any(method == "PUT" for method, _, _ in server.requests)


@pytest.mark.parametrize("immutable", [False, True])
def test_committed_but_invisible_result_is_unknown_and_retry_reconciles(dav, immutable):
    backend, server, _ = dav
    final_path = "/dav/assets/a.webp"

    def invisible(request):
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


def test_verification_download_failure_is_not_content_mismatch(dav):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(503) if request.method == "GET" else None
    with pytest.raises(StoragePublicationUnknown) as caught:
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert caught.value.stage == "verify"
    assert sum(method == "GET" for method, _, _ in server.requests) == 3
    assert server.objects["/dav/assets/a.webp"] == b"image"


def test_equal_size_wrong_content_fails_full_verification(dav):
    backend, server, _ = dav
    server.fault = lambda request: httpx.Response(200, content=b"wrong") if request.method == "GET" else None
    with pytest.raises(StorageUnavailable, match="content mismatch") as caught:
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert not isinstance(caught.value, StoragePublicationUnknown)


def test_get_404_after_stat_is_retried(dav):
    backend, server, _ = dav
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


def test_broken_verification_stream_has_bounded_retries_and_is_closed(dav):
    backend, server, sleeps = dav
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


def test_conflict_with_unreadable_destination_is_unknown(dav):
    backend, server, _ = dav
    server.objects["/dav/assets/a.webp"] = b"image"
    server.fault = lambda request: httpx.Response(503) if request.method == "GET" else None
    with pytest.raises(StoragePublicationUnknown):
        backend.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert not any(method == "PUT" for method, _, _ in server.requests)
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
