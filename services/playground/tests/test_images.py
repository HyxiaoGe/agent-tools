import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import MAX_IMAGE, create_app
from tests.test_playground import KEY, settings

FILENAME = "6928511d-1bb0-4dcf-9ba7-084af854c859.jpeg"


def test_existing_image_is_read_from_fixed_gateway_path_without_consuming_quota():
    paths = []

    def image(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, content=b"image-content", headers={"content-type": "image/jpeg", "x-secret": KEY})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(image))) as client:
        before = client.get("/api/quota").json()
        for suffix in ["", "?retry=1"]:
            response = client.get(f"/api/images/{FILENAME}{suffix}")
            assert response.status_code == 200
            assert response.content == b"image-content"
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert KEY not in str(response.headers)
        assert before == client.get("/api/quota").json()
        assert paths == [f"/image/static/images/{FILENAME}"] * 2


@pytest.mark.parametrize("filename", ["other.jpeg", "file.svg", FILENAME + ".html", "https:%2F%2Fevil.example%2Fx"])
def test_image_route_cannot_fetch_arbitrary_paths(filename):
    def reject(request):
        pytest.fail("非法文件名不应访问网关")

    with TestClient(create_app(settings(), transport=httpx.MockTransport(reject))) as client:
        assert client.get(f"/api/images/{filename}").status_code == 404


@pytest.mark.parametrize(
    "status,mime,content,expected",
    [
        (404, "text/plain", b"missing", 404),
        (302, "text/plain", b"redirect", 502),
        (200, "text/html", b"<script>bad</script>", 502),
        (200, "image/svg+xml", b"<svg/>", 502),
        (200, "image/jpeg", b"x" * (MAX_IMAGE + 1), 502),
    ],
)
def test_image_read_failures_are_bounded_and_do_not_follow_redirects(status, mime, content, expected):
    def image(request):
        assert request.url.host == "gateway"
        return httpx.Response(
            status, content=content, headers={"content-type": mime, "location": "https://evil.example"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(image))) as client:
        assert client.get(f"/api/images/{FILENAME}").status_code == expected
