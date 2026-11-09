import httpx
from fastapi.testclient import TestClient

from app.catalog import TOOLS
from app.main import create_app
from app.settings import Settings
from tests.test_playground import settings


def test_examples_cover_every_operation_and_image_is_bundled_without_any_gateway_calls():
    def reject(request):
        raise AssertionError("查看示例不应请求网关")

    for config in [Settings(_env_file=None), settings()]:
        with TestClient(create_app(config, transport=httpx.MockTransport(reject))) as client:
            before = client.get("/api/quota").json()
            samples = client.get("/api/examples").json()["samples"]
            assert set(samples) == set(TOOLS)
            for name, sample in samples.items():
                assert sample["tool"] == name
                assert sample["example"] is True
                assert sample["ok"] is True
                assert "quota" not in sample and "duration_ms" not in sample
                TOOLS[name]["model"].model_validate(sample["arguments"])
            for name in ["read_page", "resolve-library-id", "query-docs"]:
                assert "<h1>" in samples[name]["rendered_html"]
                assert "<script" not in samples[name]["rendered_html"]
            image = client.get(samples["generate_image"]["result"]["image_url"])
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
            assert image.content.startswith(b"\xff\xd8\xff")
            assert len(image.content) > 10000
            assert before == client.get("/api/quota").json()
