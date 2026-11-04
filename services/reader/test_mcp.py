"""MCP 工具 read_page：网址检查（url_safety）、给模型的正文（page_text）、失败分类、经 /mcp 调用。"""

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from fastmcp import Client

import direct_read
import main
import mcp_server
import page_text
import url_safety

PIPELINE_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pipeline.yml"

WRAPPED = """Title: Example Article
URL Source: https://example.com/a
Published Time: 2026-11-03T10:00:00Z

Markdown Content:
[Home](/) [News](/news)

# Example Article

The article body.
"""


def _response(content: str, provider: str = "jina", title: str | None = "Example Article") -> main.ReadResponse:
    return main.ReadResponse(
        url="https://example.com/a",
        title=title,
        content=content,
        content_length=len(content),
        fetch_ms=120,
        attempts=1,
        provider=provider,
    )


def _failed(kind: str, status: int = 502, upstream_status=None, retryable=False, page_status=None) -> main.ReadFailed:
    detail = {
        "kind": kind,
        "message": "x",
        "retryable": retryable,
        "upstream_status": upstream_status,
        "attempts": 1,
        "duration_ms": 1,
    }
    return main.ReadFailed(status, detail, page_status)


class CheckUrlTests(unittest.TestCase):
    def test_public_url_is_kept_without_fragment(self):
        self.assertEqual(url_safety.check_url("  https://Example.com/a?b=1#part  "), "https://Example.com/a?b=1")

    def test_invalid_urls(self):
        for url in ["", "   ", "ftp://example.com/x", "https:///path", "https://example.com:99999/", "x" * 4097]:
            with self.subTest(url=url[:40]):
                with self.assertRaises(url_safety.UrlRejected) as raised:
                    url_safety.check_url(url)
                self.assertEqual(raised.exception.code, "invalid_url")

    def test_unsafe_hosts(self):
        for url in [
            "https://user:pass@example.com/",
            "http://127.0.0.1/",
            "http://10.0.0.8:8080/admin",
            "http://192.168.1.11/",
            "http://169.254.169.254/latest/meta-data",
            "http://2130706433/",  # 127.0.0.1 的整数写法
            "http://0x7f.0.0.1/",
            "http://127.1/",
            "http://0177.0.0.1/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://localhost:8000/",
            "http://printer.local/",
            "http://db.internal/",
            "http://search-service:8080/search",  # 容器名
        ]:
            with self.subTest(url=url):
                with self.assertRaises(url_safety.UrlRejected) as raised:
                    url_safety.check_url(url)
                self.assertEqual(raised.exception.code, "unsafe_url")
                self.assertNotIn(url, raised.exception.message)

    def test_credential_like_parameters_by_exact_key(self):
        for url in [
            "https://example.com/?token=abc",
            "https://example.com/?API-Key=abc",
            "https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc",
            "https://example.com/?session=1",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(url_safety.UrlRejected) as raised:
                    url_safety.check_url(url)
                self.assertEqual(raised.exception.code, "unsafe_url")
        # Fusion 按子串拦会误伤的
        for url in [
            "https://example.com/s?keyword=ai",
            "https://example.com/?author=sean",
            "https://example.com/?zipcode=100000",
            "https://quote.example.com/?code=600519",
            "https://example.com/?page=2#section-2",
            "https://docs.example.com/api#token",  # 指向“token”一节
            "https://example.com/?token=",  # 空值
        ]:
            with self.subTest(url=url):
                url_safety.check_url(url)
        # 锚点里的令牌：去掉锚点再读，不交给上游
        self.assertEqual(url_safety.check_url("https://example.com/cb#access_token=abc"), "https://example.com/cb")

    def test_public_ip_literal_is_allowed(self):
        self.assertEqual(url_safety.check_url("http://93.184.216.34/"), "http://93.184.216.34/")


class ResolvesToPrivateTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_answer_blocks(self):
        with patch.object(
            direct_read, "_resolve_public_ip", AsyncMock(side_effect=direct_read.DirectReadSkipped("private_address"))
        ):
            self.assertTrue(await url_safety.resolves_to_private("intranet.example.com"))

    async def test_lookup_problems_do_not_block(self):
        for error in [
            direct_read.DirectReadSkipped("dns_unresolved"),
            httpx.ConnectError("doh down"),
            TimeoutError(),
        ]:
            with self.subTest(error=type(error).__name__):
                with patch.object(direct_read, "_resolve_public_ip", AsyncMock(side_effect=error)):
                    self.assertFalse(await url_safety.resolves_to_private("example.com"))

    async def test_public_answer_and_ip_hosts(self):
        resolve = AsyncMock(return_value="93.184.216.34")
        with patch.object(direct_read, "_resolve_public_ip", resolve):
            self.assertFalse(await url_safety.resolves_to_private("example.com"))
            self.assertFalse(await url_safety.resolves_to_private("93.184.216.34"))
        self.assertEqual(resolve.await_count, 1)  # IP 写法不查


class PageTextTests(unittest.TestCase):
    def test_jina_wrapper_is_removed_and_body_starts_at_matching_h1(self):
        text = page_text.page_text(WRAPPED, "Example Article")
        self.assertEqual(text.content, "# Example Article\n\nThe article body.")
        self.assertFalse(text.truncated)

    def test_without_matching_h1_the_whole_body_is_kept(self):
        text = page_text.page_text(WRAPPED, "Another Title")
        self.assertTrue(text.content.startswith("[Home](/)"))
        self.assertNotIn("URL Source", text.content)

    def test_h1_inside_code_fence_is_ignored(self):
        content = WRAPPED.replace("[Home](/) [News](/news)", "```\n# Example Article\n```\nintro")
        # 第一处匹配在代码块里，不算；正文从代码块后那个标题开始
        text = page_text.page_text(content, "Example Article")
        self.assertEqual(text.content, "# Example Article\n\nThe article body.")
        # 只有代码块里有：不算匹配，整段保留
        only_fenced = WRAPPED.replace("\n# Example Article\n", "\n```\n# Example Article\n```\n")
        self.assertTrue(page_text.page_text(only_fenced, "Example Article").content.startswith("[Home](/)"))

    def test_empty_wrapped_body(self):
        empty = "Title: x\nURL Source: https://example.com/\n\nMarkdown Content:\n\n  \n"
        with self.assertRaises(page_text.NoContent) as raised:
            page_text.page_text(empty, "x")
        self.assertEqual(raised.exception.code, "empty_content")

    def test_blocked_is_judged_from_the_header_only(self):
        blocked = (
            "Title: Just a moment...\nURL Source: https://example.com/\n"
            "Warning: This page maybe requiring CAPTCHA, please make sure you are authorized to access this page.\n\n"
            "Markdown Content:\n![Image 1](https://example.com/c.png)\n"
        )
        with self.assertRaises(page_text.NoContent) as raised:
            page_text.page_text(blocked, None)
        self.assertEqual(raised.exception.code, "access_blocked")
        # 包装头有警告但正文有字：算读到
        readable = blocked.replace("![Image 1](https://example.com/c.png)", "Real text about CAPTCHA research.")
        self.assertEqual(page_text.page_text(readable, None).content, "Real text about CAPTCHA research.")
        # 正文里讲验证码、拒绝访问的文章照样算读到
        article = "Title: t\nURL Source: https://e.com/\n\nMarkdown Content:\nWhy sites show a CAPTCHA: access denied."
        self.assertIn("CAPTCHA", page_text.page_text(article, "t").content)

    def test_plain_content_from_other_providers_is_kept(self):
        self.assertEqual(page_text.page_text("  # T\n\nbody  ", "T").content, "# T\n\nbody")
        with self.assertRaises(page_text.NoContent):
            page_text.page_text("![only](https://e.com/i.png)", None)

    def test_truncated_once_at_max_chars(self):
        text = page_text.page_text("字" * (page_text.MAX_CHARS + 500), None)
        self.assertTrue(text.truncated)
        self.assertEqual(len(text.content), page_text.MAX_CHARS)
        self.assertEqual(text.total_chars, page_text.MAX_CHARS + 500)


class ReadPageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        dns = patch.object(url_safety, "resolves_to_private", AsyncMock(return_value=False))
        self.dns = dns.start()
        self.addCleanup(dns.stop)
        self.read = AsyncMock(return_value=_response(WRAPPED))

    async def _call(self, **args):
        async with Client(mcp_server.build_mcp(self.read)) as client:
            result = await client.call_tool_mcp(mcp_server.TOOL_NAME, args)
        return result.content[0].text, result.structured_content, result.is_error

    async def test_tool_is_listed_with_description(self):
        async with Client(mcp_server.build_mcp(self.read)) as client:
            tools = await client.list_tools()
        self.assertEqual([t.name for t in tools], ["read_page"])
        self.assertEqual(tools[0].description, mcp_server.DESCRIPTION)
        self.assertEqual(set(tools[0].input_schema["properties"]), {"url", "full_page"})
        self.assertEqual(tools[0].input_schema["required"], ["url"])
        self.assertTrue(tools[0].annotations.read_only_hint)

    async def test_success_strips_wrapper_and_hides_meta_from_model(self):
        text, data, is_error = await self._call(url="https://example.com/a#top")
        self.read.assert_awaited_once_with("https://example.com/a", False)
        self.assertFalse(is_error)
        self.assertTrue(data["ok"])
        self.assertEqual(data["content"], "# Example Article\n\nThe article body.")
        self.assertEqual(data["title"], "Example Article")
        self.assertEqual(data["meta"], {"provider": "jina", "attempts": 1, "fetch_ms": 120})
        self.assertNotIn("truncated", data)
        self.assertNotIn("note", data)
        seen = json.loads(text)
        self.assertNotIn("meta", seen)
        self.assertEqual(seen["url"], "https://example.com/a")

    async def test_extracted_and_truncated_notes(self):
        self.read.return_value = _response("x" * 9000, provider="direct")
        _, data, _ = await self._call(url="https://example.com/a", full_page=False)
        self.assertTrue(data["extracted"])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["content_chars"], 9000)
        self.assertIn("first 8000 of 9000", data["note"])
        self.assertIn("full_page=true", data["note"])

    async def test_full_page_is_passed_through(self):
        await self._call(url="https://example.com/a", full_page=True)
        self.read.assert_awaited_once_with("https://example.com/a", True)

    async def test_rejected_urls_are_never_read(self):
        for url, code in [("ftp://x.com/", "invalid_url"), ("http://10.0.0.1/", "unsafe_url")]:
            with self.subTest(url=url):
                text, data, is_error = await self._call(url=url)
                self.assertFalse(is_error)
                self.assertEqual(data["error"]["code"], code)
                self.assertFalse(data["error"]["retryable"])
                self.assertTrue(text.startswith("Not read:"))
        self.read.assert_not_awaited()

    async def test_host_resolving_to_private_is_not_read(self):
        self.dns.return_value = True
        _, data, _ = await self._call(url="https://intranet.example.com/")
        self.assertEqual(data["error"]["code"], "unsafe_url")
        self.read.assert_not_awaited()

    async def test_no_content_after_wrapper(self):
        self.read.return_value = _response("Title: x\nURL Source: https://e.com/\n\nMarkdown Content:\n")
        text, data, _ = await self._call(url="https://example.com/a")
        self.assertEqual(data["error"], {"code": "empty_content", "message": mcp_server.MESSAGES["empty_content"], "retryable": False})
        self.assertIn("do not fabricate", text)

    async def test_failures_are_classified(self):
        cases = [
            (_failed("page_not_found", 404, upstream_status=404), "page_not_found", False, 404),
            (_failed("page_error", 502, upstream_status=403), "access_blocked", False, 403),
            (_failed("page_error", 502, upstream_status=503), "page_error", True, 503),
            (_failed("page_error", 502, upstream_status=400), "page_error", False, 400),
            (_failed("daily_quota_exceeded", 429), "quota_exhausted", False, None),
            (_failed("timeout", 504, retryable=True), "timeout", True, None),
            # Jina 只是超时，Firecrawl 看到原网页 403：报原网页拦了
            (_failed("timeout", 504, retryable=True, page_status=403), "access_blocked", False, 403),
            (_failed("rate_limited", 503, upstream_status=429, retryable=True), "rate_limited", True, None),
            (_failed("upstream_auth", 502, upstream_status=401), "upstream_error", False, None),
            (_failed("request_error", 502, retryable=True), "upstream_error", True, None),
        ]
        for exc, code, retryable, status in cases:
            with self.subTest(kind=exc.detail["kind"], page_status=exc.page_status):
                self.read.side_effect = exc
                text, data, is_error = await self._call(url="https://example.com/a")
                self.assertFalse(is_error)
                self.assertEqual(data["ok"], False)
                self.assertEqual(data["error"]["code"], code)
                self.assertEqual(data["error"]["retryable"], retryable)
                self.assertEqual(data["error"].get("status"), status)
                self.assertIn("do not fabricate", text)
                if code == "quota_exhausted":
                    self.assertIn("Do not keep trying other pages", text)
                if code == "page_not_found":
                    self.assertIn("Do not read this URL again", text)


class PackagingTests(unittest.TestCase):
    def test_container_ships_mcp_modules(self):
        dockerfile = (Path(__file__).parent / "Dockerfile").read_text()
        for module in ("mcp_server.py", "page_text.py", "url_safety.py"):
            self.assertIn(module, dockerfile)
        self.assertIn("fastmcp==", (Path(__file__).parent / "requirements.txt").read_text())

    def test_workflow_runs_mcp_smoke_after_rest_smoke(self):
        workflow = PIPELINE_WORKFLOW.read_text()
        smoke = 'docker exec -i reader-service python - < "$DEPLOY_DIR/services/reader/scripts/smoke_mcp.py"'
        self.assertIn(smoke, workflow)
        self.assertLess(workflow.index("scripts/smoke_read.py"), workflow.index(smoke))
        self.assertTrue((Path(__file__).parent / "scripts" / "smoke_mcp.py").is_file())


class MountedMcpTests(unittest.TestCase):
    def test_stateless_mcp_is_served_next_to_rest(self):
        with TestClient(main.app) as client:
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            health = client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = next(line[5:] for line in response.text.splitlines() if line.startswith("data:"))
        self.assertEqual([t["name"] for t in json.loads(data)["result"]["tools"]], ["read_page"])
        self.assertEqual(health.status_code, 200)


if __name__ == "__main__":
    unittest.main()
