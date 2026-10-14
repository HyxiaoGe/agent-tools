import asyncio
import importlib.util
import io
import json
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import direct_read
import main


class ReaderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.url = "https://example.com/private?token=secret"
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.__aexit__.return_value = None
        self.client_patch = patch("main.httpx.AsyncClient", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        main._account_failure = None
        self.addCleanup(setattr, main, "_account_failure", None)
        tavily_pool_patch = patch("main._tavily_pool", main.TavilyKeyPool([]))
        tavily_pool_patch.start()
        self.addCleanup(tavily_pool_patch.stop)
        firecrawl_pool_patch = patch("main._firecrawl_pool", main.FirecrawlKeyPool([]))
        firecrawl_pool_patch.start()
        self.addCleanup(firecrawl_pool_patch.stop)
        # 这组用例覆盖 Tavily/Jina 链路；直接读取另见 DirectReadTests。
        direct_patch = patch("main.read_direct", new=AsyncMock(return_value=None))
        self.read_direct = direct_patch.start()
        self.addCleanup(direct_patch.stop)

    @staticmethod
    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    @staticmethod
    def _status_error(status_code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        response = httpx.Response(status_code, request=request)
        return httpx.HTTPStatusError(
            f"上游返回 {status_code}", request=request, response=response
        )

    async def test_timeout_retries_once_and_returns_structured_error(self):
        self.client.get.side_effect = [
            httpx.ReadTimeout("第一次超时"),
            httpx.ReadTimeout("第二次超时"),
        ]

        with patch("main.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(
            raised.exception.detail,
            {
                "kind": "timeout",
                "message": "Jina Reader 请求超时",
                "retryable": True,
                "upstream_status": None,
                "attempts": 2,
                "duration_ms": unittest.mock.ANY,
            },
        )
        self.assertEqual(self.client.get.await_count, 2)
        sleep.assert_awaited_once()

    async def test_upstream_auth_does_not_retry(self):
        for status_code in (401, 402, 403):
            with self.subTest(status_code=status_code):
                self.client.get.reset_mock()
                self.client.get.side_effect = self._status_error(status_code)

                with self.assertRaises(HTTPException) as raised:
                    await main.read_url(self.url)

                self.assertEqual(raised.exception.status_code, 502)
                self.assertEqual(raised.exception.detail["kind"], "upstream_auth")
                self.assertFalse(raised.exception.detail["retryable"])
                self.assertEqual(
                    raised.exception.detail["upstream_status"], status_code
                )
                self.assertEqual(raised.exception.detail["attempts"], 1)
                self.assertEqual(self.client.get.await_count, 1)

    async def test_account_failure_turns_health_degraded_until_next_success(self):
        self.assertEqual(await main.health(), {"status": "ok"})
        self.client.get.side_effect = self._status_error(402)

        with self.assertRaises(HTTPException):
            await main.read_url(self.url)

        degraded = await main.health()
        self.assertEqual(degraded.status_code, 503)
        body = json.loads(degraded.body)
        self.assertEqual(body["jina_account"]["upstream_status"], 402)
        self.assertEqual(body["jina_account"]["message"], "Jina Reader 账户余额不足")
        self.assertNotIn("secret", degraded.body.decode())

        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        self.client.get.side_effect = None
        self.client.get.return_value = httpx.Response(
            200, request=request, text="Title: ok\n\nbody"
        )
        await main.read_url(self.url)
        self.assertEqual(await main.health(), {"status": "ok"})

    async def test_page_level_failures_do_not_degrade_health(self):
        for status_code in (400, 404, 429, 500):
            with self.subTest(status_code=status_code):
                self.client.get.side_effect = self._status_error(status_code)
                with (
                    patch("main.asyncio.sleep", new=AsyncMock()),
                    self.assertRaises(HTTPException),
                ):
                    await main.read_url(self.url)
                self.assertEqual(await main.health(), {"status": "ok"})

    async def test_rate_limit_retries_once(self):
        self.client.get.side_effect = [
            self._status_error(429),
            self._status_error(429),
        ]

        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["kind"], "rate_limited")
        self.assertTrue(raised.exception.detail["retryable"])
        self.assertEqual(raised.exception.detail["upstream_status"], 429)
        self.assertEqual(raised.exception.detail["attempts"], 2)
        self.assertEqual(self.client.get.await_count, 2)

    async def test_upstream_5xx_retries_once(self):
        self.client.get.side_effect = [
            self._status_error(503),
            self._status_error(503),
        ]

        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["kind"], "upstream_error")
        self.assertTrue(raised.exception.detail["retryable"])
        self.assertEqual(raised.exception.detail["upstream_status"], 503)
        self.assertEqual(raised.exception.detail["attempts"], 2)

    async def test_fast_request_error_retries_once(self):
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        self.client.get.side_effect = httpx.ConnectError(
            "连接失败 token=secret", request=request
        )

        with patch("main.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["kind"], "request_error")
        self.assertTrue(raised.exception.detail["retryable"])
        self.assertIsNone(raised.exception.detail["upstream_status"])
        self.assertEqual(raised.exception.detail["attempts"], 2)
        self.assertEqual(raised.exception.detail["message"], "Jina Reader 请求失败")
        self.assertNotIn("secret", str(raised.exception.detail))
        self.assertEqual(self.client.get.await_count, 2)
        sleep.assert_awaited_once()

    async def test_unknown_error_is_structured_and_does_not_leak_message(self):
        self.client.get.side_effect = RuntimeError(
            "意外失败 https://example.com/private?token=secret"
        )

        with self.assertLogs("reader_service", level=logging.WARNING) as logs:
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["kind"], "request_error")
        self.assertEqual(raised.exception.detail["message"], "Jina Reader 请求失败")
        self.assertFalse(raised.exception.detail["retryable"])
        self.assertEqual(raised.exception.detail["attempts"], 1)
        self.assertNotIn("secret", str(raised.exception.detail))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertEqual(self.client.get.await_count, 1)

    async def test_invalid_url_does_not_echo_input(self):
        invalid_url = "javascript:alert('secret')"

        with self.assertRaises(HTTPException) as raised:
            await main.read_url(invalid_url)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "URL 格式无效")
        self.assertNotIn("secret", raised.exception.detail)
        self.client.get.assert_not_awaited()

    async def test_malformed_ipv6_returns_fixed_422_without_echo(self):
        invalid_url = "https://[::1/private?token=secret"

        with self.assertRaises(HTTPException) as raised:
            await main.read_url(invalid_url)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "URL 格式无效")
        self.assertNotIn("secret", raised.exception.detail)
        self.client.get.assert_not_awaited()

    async def test_invalid_port_returns_fixed_422_without_upstream_request(self):
        invalid_url = "https://example.com:not-a-port/private?token=secret"

        with self.assertRaises(HTTPException) as raised:
            await main.read_url(invalid_url)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "URL 格式无效")
        self.assertNotIn("secret", raised.exception.detail)
        self.client.get.assert_not_awaited()

    async def test_other_upstream_4xx_does_not_retry(self):
        self.client.get.side_effect = self._status_error(400)

        with self.assertRaises(HTTPException) as raised:
            await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["kind"], "upstream_error")
        self.assertFalse(raised.exception.detail["retryable"])
        self.assertEqual(raised.exception.detail["upstream_status"], 400)
        self.assertEqual(raised.exception.detail["attempts"], 1)
        self.assertEqual(self.client.get.await_count, 1)

    async def test_retry_success_reports_attempts(self):
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        success = httpx.Response(
            200,
            text="# 示例标题\n正文",
            headers={"x-title": "Example Title"},
            request=request,
        )
        self.client.get.side_effect = [httpx.ReadTimeout("超时"), success]

        with patch("main.asyncio.sleep", new=AsyncMock()):
            response = await main.read_url(self.url)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(response.title, "Example Title")
        self.assertEqual(response.content, "# 示例标题\n正文")

    async def test_failure_log_does_not_contain_full_url_or_query(self):
        self.client.get.side_effect = self._status_error(401)

        with self.assertLogs("reader_service", level=logging.WARNING) as logs:
            with self.assertRaises(HTTPException):
                await main.read_url(self.url)

        joined = "\n".join(logs.output)
        self.assertIn("domain=example.com", joined)
        self.assertIn("kind=upstream_auth", joined)
        self.assertNotIn("private", joined)
        self.assertNotIn("secret", joined)

    def test_container_disables_access_log_and_keeps_port_8091(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text()

        self.assertIn("EXPOSE 8091", dockerfile)
        self.assertIn('"--port", "8091", "--no-access-log"', dockerfile)

    def test_workflow_runs_explicit_tests_before_deploy(self):
        workflow = (
            Path(__file__).with_name(".github") / "workflows" / "deploy.yml"
        ).read_text()

        explicit_test_command = "python -m unittest -v test_main"
        self.assertIn(explicit_test_command, workflow)
        self.assertNotIn("python -m unittest discover", workflow)
        self.assertLess(
            workflow.index(explicit_test_command),
            workflow.index("docker build -t reader-service ."),
        )

    def test_workflow_runs_real_read_smoke_after_liveness_check(self):
        workflow = (
            Path(__file__).with_name(".github") / "workflows" / "deploy.yml"
        ).read_text()

        smoke_command = "python scripts/smoke_read.py"
        self.assertIn(smoke_command, workflow)
        self.assertLess(
            workflow.index("curl -sf http://localhost:8091/health"),
            workflow.index(smoke_command),
        )

    def test_smoke_script_validates_content_and_attempts_without_leaking_target(self):
        script_path = Path(__file__).with_name("scripts") / "smoke_read.py"
        self.assertTrue(script_path.is_file())
        spec = importlib.util.spec_from_file_location("reader_smoke_read", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        smoke_read = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke_read)

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"content":"ok","attempts":1}'

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(smoke_read.request, "urlopen", return_value=FakeResponse()),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = smoke_read.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("reader smoke passed", stdout.getvalue())
        self.assertNotIn(smoke_read.SMOKE_TARGET, stdout.getvalue())
        self.assertNotIn(smoke_read.SMOKE_TARGET, stderr.getvalue())

    def test_smoke_script_failure_only_outputs_safe_diagnostics(self):
        script_path = Path(__file__).with_name("scripts") / "smoke_read.py"
        self.assertTrue(script_path.is_file())
        spec = importlib.util.spec_from_file_location(
            "reader_smoke_read_failure", script_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        smoke_read = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke_read)

        secret_target = "https://example.com/private?token=secret"
        upstream_error = smoke_read.error.HTTPError(
            secret_target,
            502,
            "JINA_API_KEY=secret",
            {},
            io.BytesIO(
                b'{"detail":{"kind":"upstream_auth","upstream_status":401,'
                b'"message":"JINA_API_KEY=secret"}}'
            ),
        )
        stderr = io.StringIO()
        with (
            patch.object(smoke_read, "SMOKE_TARGET", secret_target),
            patch.object(smoke_read.request, "urlopen", side_effect=upstream_error),
            redirect_stderr(stderr),
        ):
            exit_code = smoke_read.main()

        output = stderr.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("http_status=502", output)
        self.assertIn("kind=upstream_auth", output)
        self.assertIn("upstream_status=401", output)
        self.assertNotIn(secret_target, output)
        self.assertNotIn("secret", output)
        self.assertNotIn("JINA_API_KEY", output)

    def test_key_check_script_never_prints_keys(self):
        script_path = Path(__file__).with_name("scripts") / "check_tavily_keys.py"
        spec = importlib.util.spec_from_file_location("check_tavily_keys", script_path)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        request = httpx.Request("GET", check.TAVILY_USAGE_URL)
        responses = {
            "tvly-good": httpx.Response(
                200,
                json={"account": {"current_plan": "Researcher", "plan_usage": 6,
                                  "plan_limit": 1000}},
                request=request,
            ),
            "tvly-full": httpx.Response(
                200,
                json={"account": {"plan_usage": 1000, "plan_limit": 1000}},
                request=request,
            ),
            "tvly-bad": httpx.Response(401, request=request),
        }

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get(self, _url, headers):
                return responses[headers["Authorization"].removeprefix("Bearer ")]

        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"TAVILY_API_KEYS": "tvly-good, tvly-full,tvly-bad,tvly-good"},
            ),
            patch.object(check.httpx, "Client", FakeClient),
            redirect_stdout(stdout),
        ):
            exit_code = check.main()

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("key#1 ok plan=Researcher usage=6/1000", output)
        self.assertIn("key#2 exhausted", output)
        self.assertIn("key#3 http_status=401", output)
        self.assertIn("key#4 duplicate", output)
        self.assertIn("usable=1/4", output)
        self.assertNotIn("tvly-", output)

    def test_container_ships_scripts_and_pool_secret(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text()
        workflow = (
            Path(__file__).with_name(".github") / "workflows" / "deploy.yml"
        ).read_text()

        self.assertIn("COPY scripts/ scripts/", dockerfile)
        self.assertIn("-e TAVILY_API_KEYS=${{ secrets.TAVILY_API_KEYS }}", workflow)
        self.assertIn("-e FIRECRAWL_API_KEYS=${{ secrets.FIRECRAWL_API_KEYS }}", workflow)

    def test_firecrawl_key_check_script_never_prints_keys(self):
        script_path = Path(__file__).with_name("scripts") / "check_firecrawl_keys.py"
        spec = importlib.util.spec_from_file_location("check_firecrawl_keys", script_path)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        request = httpx.Request("GET", check.FIRECRAWL_CREDIT_URL)
        responses = {
            "fc-good": httpx.Response(
                200,
                json={"data": {"remainingCredits": 76000, "planCredits": 1000}},
                request=request,
            ),
            "fc-empty": httpx.Response(
                200, json={"data": {"remainingCredits": 0}}, request=request
            ),
            "fc-bad": httpx.Response(401, request=request),
        }

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get(self, _url, headers):
                return responses[headers["Authorization"].removeprefix("Bearer ")]

        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"FIRECRAWL_API_KEYS": "fc-good, fc-empty,fc-bad,fc-good"},
            ),
            patch.object(check.httpx, "Client", FakeClient),
            redirect_stdout(stdout),
        ):
            exit_code = check.main()

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("key#1 ok remaining=76000 plan=1000", output)
        self.assertIn("key#2 exhausted", output)
        self.assertIn("key#3 http_status=401", output)
        self.assertIn("key#4 duplicate", output)
        self.assertIn("usable=1/4", output)
        self.assertNotIn("fc-", output)

    async def test_failure_without_retry_window_does_not_retry(self):
        self.client.get.side_effect = httpx.ReadTimeout("超时")

        with (
            patch("main.READ_TOTAL_TIMEOUT", 0.5),
            patch("main.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.detail["kind"], "timeout")
        self.assertEqual(raised.exception.detail["attempts"], 1)
        self.assertEqual(self.client.get.await_count, 1)
        sleep.assert_not_awaited()

    async def test_total_deadline_expired_during_backoff_does_not_start_second_attempt(
        self,
    ):
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        success = httpx.Response(200, text="不应被读取", request=request)
        self.client.get.side_effect = [httpx.ReadTimeout("第一次超时"), success]

        with (
            patch("main.READ_TOTAL_TIMEOUT", 0.2),
            patch("main.JINA_MIN_RETRY_WINDOW", -1.0),
            patch("main.JINA_RETRY_BACKOFF", 0.5),
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["kind"], "timeout")
        self.assertEqual(raised.exception.detail["attempts"], 1)
        self.assertEqual(self.client.get.await_count, 1)

    async def test_slow_jina_is_abandoned_at_total_deadline(self):
        self.client.get.side_effect = self._hang

        started = asyncio.get_running_loop().time()
        with patch("main.READ_TOTAL_TIMEOUT", 0.2):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["kind"], "timeout")
        self.assertEqual(raised.exception.detail["attempts"], 1)

    @staticmethod
    def _tavily_response(results: list, status_code: int = 200) -> httpx.Response:
        request = httpx.Request("POST", main.TAVILY_EXTRACT_URL)
        return httpx.Response(
            status_code, json={"results": results, "failed_results": []}, request=request
        )

    @staticmethod
    def _jina_success(text: str = "# Jina 标题\n正文") -> httpx.Response:
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        return httpx.Response(200, text=text, request=request)

    async def test_tavily_success_wins_while_jina_still_reading(self):
        self.client.post.return_value = self._tavily_response(
            [{"url": self.url, "title": "Tavily 标题", "raw_content": "正文内容"}]
        )
        self.client.get.side_effect = self._hang

        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "tavily")
        self.assertEqual(response.content, "正文内容")
        self.assertEqual(response.title, "Tavily 标题")
        self.assertEqual(response.attempts, 1)
        call = self.client.post.await_args
        self.assertEqual(call.args[0], main.TAVILY_EXTRACT_URL)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer tvly-test")
        self.assertEqual(call.kwargs["json"]["urls"], [self.url])
        self.assertEqual(call.kwargs["json"]["extract_depth"], "basic")

    async def test_tavily_without_title_uses_first_heading(self):
        self.client.post.return_value = self._tavily_response(
            [{"url": self.url, "raw_content": "# 页面标题\n正文"}]
        )
        self.client.get.side_effect = self._hang

        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])):
            response = await main.read_url(self.url)

        self.assertEqual(response.title, "页面标题")

    async def test_tavily_empty_content_falls_back_to_jina(self):
        for results in ([], [{"url": self.url, "raw_content": "  "}]):
            with self.subTest(results=results):
                self.client.post.reset_mock()
                self.client.get.reset_mock()
                self.client.post.return_value = self._tavily_response(results)
                self.client.get.return_value = self._jina_success()

                with (
                    patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])),
                    self.assertLogs("reader_service", level=logging.WARNING) as logs,
                ):
                    response = await main.read_url(self.url)

                self.assertEqual(response.provider, "jina")
                self.assertEqual(response.title, "Jina 标题")
                self.assertEqual(response.attempts, 2)
                self.assertEqual(self.client.get.await_count, 1)
                joined = "\n".join(logs.output)
                self.assertIn("reader_tavily_fallback", joined)
                self.assertIn("kind=empty_content", joined)
                self.assertNotIn("secret", joined)

    async def test_tavily_errors_fall_back_to_jina(self):
        errors = {
            "timeout": httpx.ReadTimeout("超时"),
            "upstream_error": self._status_error(500),
            "request_error": httpx.ConnectError("连不上"),
        }
        for kind, exc in errors.items():
            with self.subTest(kind=kind):
                self.client.post.reset_mock()
                self.client.get.reset_mock()
                self.client.post.side_effect = exc
                self.client.get.return_value = self._jina_success()

                with (
                    patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])),
                    self.assertLogs("reader_service", level=logging.WARNING) as logs,
                ):
                    response = await main.read_url(self.url)

                self.assertEqual(response.provider, "jina")
                self.assertIn(f"kind={kind}", "\n".join(logs.output))
                self.assertEqual(await main.health(), {"status": "ok"})

    async def test_slow_direct_read_hedges_to_providers(self):
        async def slow_direct(*_args):
            await asyncio.sleep(5)

        self.read_direct.side_effect = slow_direct
        self.client.get.return_value = self._jina_success()

        started = asyncio.get_running_loop().time()
        with patch("main.DIRECT_HEDGE_DELAY", 0.05):
            response = await main.read_url(self.url)

        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertEqual(response.provider, "jina")

    async def test_failed_direct_read_starts_providers_without_waiting(self):
        self.client.get.return_value = self._jina_success()

        started = asyncio.get_running_loop().time()
        with patch("main.DIRECT_HEDGE_DELAY", 5.0):
            response = await main.read_url(self.url)

        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertEqual(response.provider, "jina")

    async def test_direct_read_finishing_after_hedge_can_still_win(self):
        async def late_direct(*_args):
            await asyncio.sleep(0.1)
            return direct_read.DirectPage(content="直接读取的正文", title="直接标题")

        self.read_direct.side_effect = late_direct
        self.client.get.side_effect = self._hang

        with patch("main.DIRECT_HEDGE_DELAY", 0.01):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "direct")
        self.assertEqual(response.content, "直接读取的正文")

    async def test_jina_fast_error_waits_for_tavily(self):
        async def slow_tavily(*_args, **_kwargs):
            await asyncio.sleep(0.1)
            return self._tavily_response([{"url": self.url, "raw_content": "正文"}])

        self.client.post.side_effect = slow_tavily
        self.client.get.side_effect = self._status_error(404)

        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "tavily")

    async def test_all_providers_failing_reports_jina_error(self):
        self.client.post.return_value = self._tavily_response([])
        self.client.get.side_effect = self._status_error(404)

        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])):
            with self.assertRaises(HTTPException) as raised:
                await main.read_url(self.url)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["upstream_status"], 404)
        self.assertEqual(raised.exception.detail["attempts"], 2)

    def _tavily_ok(self) -> httpx.Response:
        return self._tavily_response([{"url": self.url, "raw_content": "正文"}])

    def _used_keys(self) -> list[str]:
        return [
            call.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            for call in self.client.post.await_args_list
        ]

    async def test_pool_rotates_keys_across_requests(self):
        self.client.post.return_value = self._tavily_ok()
        self.client.get.side_effect = self._hang
        pool = main.TavilyKeyPool(["k1", "k2", "k3"])

        with patch("main._tavily_pool", pool):
            for _ in range(4):
                await main.read_url(self.url)

        self.assertEqual(self._used_keys(), ["k1", "k2", "k3", "k1"])

    async def test_account_failure_switches_key_within_same_request(self):
        for status_code in (401, 429, 432, 433):
            with self.subTest(status_code=status_code):
                self.client.post.reset_mock()
                self.client.post.side_effect = [
                    self._status_error(status_code),
                    self._tavily_ok(),
                ]
                self.client.get.side_effect = self._hang
                pool = main.TavilyKeyPool(["k1", "k2"])

                with patch("main._tavily_pool", pool):
                    response = await main.read_url(self.url)

                self.assertEqual(response.provider, "tavily")
                self.assertEqual(self._used_keys(), ["k1", "k2"])
                self.assertEqual(pool.available_count(), 1)

    async def test_unavailable_key_is_skipped_until_cooldown_expires(self):
        pool = main.TavilyKeyPool(["k1", "k2"])
        self.client.post.side_effect = [self._status_error(429), self._tavily_ok()]
        self.client.get.side_effect = self._hang

        with (
            patch("main._tavily_pool", pool),
            patch("main._wall_time", return_value=1000.0),
        ):
            await main.read_url(self.url)
            self.client.post.reset_mock()
            self.client.post.side_effect = None
            self.client.post.return_value = self._tavily_ok()
            await main.read_url(self.url)
            await main.read_url(self.url)
            self.assertEqual(self._used_keys(), ["k2", "k2"])

        self.client.post.reset_mock()
        with (
            patch("main._tavily_pool", pool),
            patch("main._wall_time", return_value=1061.0),
        ):
            await main.read_url(self.url)
            await main.read_url(self.url)
        self.assertEqual(sorted(self._used_keys()), ["k1", "k2"])

    async def test_invalid_key_stays_disabled(self):
        pool = main.TavilyKeyPool(["k1", "k2"])
        self.client.post.side_effect = [self._status_error(401), self._tavily_ok()]
        self.client.get.side_effect = self._hang

        with (
            patch("main._tavily_pool", pool),
            patch("main._wall_time", return_value=1000.0),
        ):
            await main.read_url(self.url)

        self.client.post.reset_mock()
        self.client.post.side_effect = None
        self.client.post.return_value = self._tavily_ok()
        with (
            patch("main._tavily_pool", pool),
            patch("main._wall_time", return_value=10**9),
        ):
            for _ in range(3):
                await main.read_url(self.url)
        self.assertEqual(self._used_keys(), ["k2", "k2", "k2"])

    async def test_all_keys_unavailable_degrades_health_and_falls_back(self):
        pool = main.TavilyKeyPool(["k1", "k2"])
        self.client.post.side_effect = [self._status_error(432), self._status_error(401)]
        self.client.get.return_value = self._jina_success()

        with patch("main._tavily_pool", pool):
            response = await main.read_url(self.url)
            self.assertEqual(response.provider, "jina")
            degraded = await main.health()

            self.assertEqual(degraded.status_code, 503)
            body = json.loads(degraded.body)
            self.assertEqual(body["tavily_account"]["total"], 2)
            self.assertEqual(body["tavily_account"]["available"], 0)
            self.assertEqual(
                body["tavily_account"]["key_statuses"], {"1": 432, "2": 401}
            )
            self.assertNotIn("jina_account", body)
            self.assertNotIn("k1", degraded.body.decode())

            self.client.post.reset_mock()
            self.client.get.reset_mock()
            self.client.get.return_value = self._jina_success()
            with self.assertLogs("reader_service", level=logging.WARNING) as logs:
                response = await main.read_url(self.url)
            self.assertEqual(response.provider, "jina")
            self.client.post.assert_not_awaited()
            self.assertIn("kind=no_available_key", "\n".join(logs.output))

    async def test_page_level_tavily_failure_does_not_burn_other_keys(self):
        pool = main.TavilyKeyPool(["k1", "k2"])
        self.client.post.side_effect = self._status_error(500)
        self.client.get.return_value = self._jina_success()

        with patch("main._tavily_pool", pool):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "jina")
        self.assertEqual(self._used_keys(), ["k1"])
        self.assertEqual(pool.available_count(), 2)

    def _firecrawl_response(
        self, markdown: str = "# 页面标题\n正文", page_status: int = 200, title=None
    ) -> httpx.Response:
        request = httpx.Request("POST", main.FIRECRAWL_SCRAPE_URL)
        metadata = {"statusCode": page_status}
        if title is not None:
            metadata["title"] = title
        return httpx.Response(
            200,
            json={"success": True, "data": {"markdown": markdown, "metadata": metadata}},
            request=request,
        )

    async def test_firecrawl_success_wins_while_jina_still_reading(self):
        self.client.post.return_value = self._firecrawl_response(title="Firecrawl 标题")
        self.client.get.side_effect = self._hang

        with patch("main._firecrawl_pool", main.FirecrawlKeyPool(["fc-test"])):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "firecrawl")
        self.assertEqual(response.title, "Firecrawl 标题")
        self.assertEqual(response.content, "# 页面标题\n正文")
        call = self.client.post.await_args
        self.assertEqual(call.args[0], main.FIRECRAWL_SCRAPE_URL)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer fc-test")
        self.assertEqual(call.kwargs["json"]["url"], self.url)
        self.assertEqual(call.kwargs["json"]["formats"], ["markdown"])
        self.assertTrue(call.kwargs["json"]["onlyMainContent"])

    async def test_firecrawl_full_mode_requests_whole_page(self):
        self.client.post.return_value = self._firecrawl_response()
        self.client.get.side_effect = self._hang

        with patch("main._firecrawl_pool", main.FirecrawlKeyPool(["fc-test"])):
            response = await main.read_url(self.url, full=True)

        self.assertEqual(response.provider, "firecrawl")
        self.assertFalse(self.client.post.await_args.kwargs["json"]["onlyMainContent"])

    async def test_firecrawl_error_page_is_not_used_as_content(self):
        self.client.post.return_value = self._firecrawl_response("找不到页面", 404)
        self.client.get.return_value = self._jina_success()

        with (
            patch("main._firecrawl_pool", main.FirecrawlKeyPool(["fc-test"])),
            self.assertLogs("reader_service", level=logging.WARNING) as logs,
        ):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "jina")
        joined = "\n".join(logs.output)
        self.assertIn("reader_firecrawl_fallback", joined)
        self.assertIn("kind=page_status", joined)

    async def test_tavily_and_firecrawl_race_together(self):
        async def slow_tavily(*_args, **_kwargs):
            await asyncio.sleep(0.2)
            return self._tavily_response([{"url": self.url, "raw_content": "正文"}])

        async def post(target, **kwargs):
            if target == main.FIRECRAWL_SCRAPE_URL:
                return self._firecrawl_response()
            return await slow_tavily()

        self.client.post.side_effect = post
        self.client.get.side_effect = self._hang

        with (
            patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])),
            patch("main._firecrawl_pool", main.FirecrawlKeyPool(["fc-test"])),
        ):
            response = await main.read_url(self.url)

        self.assertEqual(response.provider, "firecrawl")
        self.assertEqual(response.attempts, 2)

    async def test_firecrawl_uses_keys_in_order_and_falls_over_on_rate_limit(self):
        pool = main.FirecrawlKeyPool(["k1", "k2", "k3"])
        self.client.get.side_effect = self._hang

        with (
            patch("main._firecrawl_pool", pool),
            patch("main._wall_time", return_value=1000.0),
        ):
            self.client.post.return_value = self._firecrawl_response()
            await main.read_url(self.url)
            await main.read_url(self.url)
            self.assertEqual(self._used_keys(), ["k1", "k1"])

            self.client.post.reset_mock()
            self.client.post.return_value = None
            self.client.post.side_effect = [
                self._status_error(429),
                self._firecrawl_response(),
                self._firecrawl_response(),
            ]
            response = await main.read_url(self.url)
            self.assertEqual(response.provider, "firecrawl")
            await main.read_url(self.url)
            self.assertEqual(self._used_keys(), ["k1", "k2", "k2"])

        self.client.post.reset_mock()
        self.client.post.side_effect = None
        self.client.post.return_value = self._firecrawl_response()
        with (
            patch("main._firecrawl_pool", pool),
            patch("main._wall_time", return_value=1031.0),
        ):
            await main.read_url(self.url)
        self.assertEqual(self._used_keys(), ["k1"])

    async def test_firecrawl_page_level_failures_do_not_burn_keys(self):
        for status_code in (403, 408, 500):
            with self.subTest(status_code=status_code):
                pool = main.FirecrawlKeyPool(["k1", "k2"])
                self.client.post.reset_mock()
                self.client.post.side_effect = self._status_error(status_code)
                self.client.get.return_value = self._jina_success()

                with patch("main._firecrawl_pool", pool):
                    response = await main.read_url(self.url)

                self.assertEqual(response.provider, "jina")
                self.assertEqual(self._used_keys(), ["k1"])
                self.assertEqual(pool.available_count(), 2)

    async def test_all_firecrawl_keys_unavailable_degrades_health(self):
        pool = main.FirecrawlKeyPool(["k1", "k2"])
        self.client.post.side_effect = [self._status_error(402), self._status_error(401)]
        self.client.get.return_value = self._jina_success()

        with patch("main._firecrawl_pool", pool):
            response = await main.read_url(self.url)
            self.assertEqual(response.provider, "jina")
            degraded = await main.health()

        self.assertEqual(degraded.status_code, 503)
        body = json.loads(degraded.body)
        self.assertEqual(body["firecrawl_account"]["key_statuses"], {"1": 402, "2": 401})
        self.assertIn("Firecrawl", body["firecrawl_account"]["message"])
        self.assertNotIn("tavily_account", body)
        self.assertNotIn("k1", degraded.body.decode())

    async def test_client_uses_split_timeout_configuration(self):
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com")
        self.client.get.return_value = httpx.Response(200, text="正文", request=request)

        await main.read_url(self.url)

        timeout = main.httpx.AsyncClient.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, main.JINA_CONNECT_TIMEOUT)
        self.assertLessEqual(timeout.read, main.READ_TOTAL_TIMEOUT)
        self.assertGreater(timeout.read, main.READ_TOTAL_TIMEOUT - 1)
        self.assertEqual(timeout.write, main.JINA_WRITE_TIMEOUT)
        self.assertEqual(timeout.pool, main.JINA_POOL_TIMEOUT)
        self.assertLessEqual(main.READ_TOTAL_TIMEOUT, 10)



ARTICLE_HTML = (
    "<html><head><title>测试文章标题</title>"
    '<meta property="og:site_name" content="测试站">'
    '<meta property="article:published_time" content="2026-10-01T08:00:00+08:00">'
    "</head><body>"
    "<nav>" + "".join(f'<a href="/c/{i}">栏目{i}</a>' for i in range(200)) + "</nav>"
    "<article><h1>测试文章标题</h1>"
    + "".join(
        f"<p>第{i}段正文：铁路部门预计今日发送旅客两千万人次，热门方向余票紧张，建议旅客合理安排出行时间并使用候补购票功能。</p>"
        for i in range(8)
    )
    + "</article><footer>版权所有</footer></body></html>"
)


class DirectReadTests(unittest.IsolatedAsyncioTestCase):
    """直接读取：只连 DoH 核验过的公网 IP，每一跳重新核验，任何不满足都回退。"""

    def setUp(self):
        self.dns = {"news.example.com": ["93.184.216.34"]}
        self.pages = {}
        self.requests = []
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.host == "223.5.5.5":
                name = request.url.params["name"]
                rtype = request.url.params["type"]
                answers = [
                    {"type": 28 if ":" in ip else 1, "data": ip}
                    for ip in self.dns.get(name, [])
                    if (":" in ip) == (rtype == "AAAA")
                ]
                return httpx.Response(200, json={"Answer": answers})
            key = (request.headers["host"], request.url.path)
            return self.pages.get(key) or httpx.Response(404, text="missing")

        def make_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        client_patch = patch("direct_read.httpx.AsyncClient", side_effect=make_client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def _html(self, host: str, path: str, body: str = ARTICLE_HTML, **headers):
        self.pages[(host, path)] = httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/html; charset=utf-8", **headers}
        )

    def _page_requests(self):
        return [r for r in self.requests if r.url.host != "223.5.5.5"]

    async def test_extracts_article_via_pinned_public_ip(self):
        self._html("news.example.com", "/a")

        page = await direct_read.read_direct("https://news.example.com/a?x=1#frag", "news.example.com")

        self.assertIsNotNone(page)
        self.assertIn("第0段正文", page.content)
        self.assertNotIn("栏目199", page.content)
        self.assertEqual(page.title, "测试文章标题")
        [request] = self._page_requests()
        self.assertEqual(request.url.host, "93.184.216.34")
        self.assertEqual(request.url.query, b"x=1")
        self.assertEqual(request.headers["host"], "news.example.com")
        self.assertEqual(request.extensions["sni_hostname"], "news.example.com")

    async def test_private_dns_answer_is_not_fetched(self):
        self.dns["news.example.com"] = ["192.168.1.11"]
        self._html("news.example.com", "/a")

        page = await direct_read.read_direct("https://news.example.com/a", "news.example.com")

        self.assertIsNone(page)
        self.assertEqual(self._page_requests(), [])

    async def test_any_private_record_rejects_host(self):
        self.dns["news.example.com"] = ["93.184.216.34", "10.0.0.5"]
        self._html("news.example.com", "/a")

        self.assertIsNone(await direct_read.read_direct("https://news.example.com/a", "d"))
        self.assertEqual(self._page_requests(), [])

    async def test_clash_fake_ip_range_is_not_trusted(self):
        self.dns["news.example.com"] = ["198.18.1.15"]

        self.assertIsNone(await direct_read.read_direct("https://news.example.com/a", "d"))
        self.assertEqual(self._page_requests(), [])

    async def test_private_ip_literal_skips_dns_and_fetch(self):
        self.assertIsNone(await direct_read.read_direct("http://192.168.1.11/admin", "d"))
        self.assertIsNone(await direct_read.read_direct("http://[::1]/admin", "d"))
        self.assertEqual(self.requests, [])

    async def test_redirect_to_private_host_is_rejected(self):
        self.dns["internal.example.com"] = ["127.0.0.1"]
        self.pages[("news.example.com", "/a")] = httpx.Response(
            302, headers={"location": "http://internal.example.com/secret"}
        )
        self._html("internal.example.com", "/secret")

        self.assertIsNone(await direct_read.read_direct("https://news.example.com/a", "d"))
        self.assertEqual([r.url.path for r in self._page_requests()], ["/a"])

    async def test_redirect_to_public_host_is_verified_again(self):
        self.dns["www.example.org"] = ["93.184.216.35"]
        self.pages[("news.example.com", "/a")] = httpx.Response(
            301, headers={"location": "https://www.example.org/b"}
        )
        self._html("www.example.org", "/b")

        page = await direct_read.read_direct("https://news.example.com/a", "d")

        self.assertIsNotNone(page)
        resolved = [r.url.params["name"] for r in self.requests if r.url.host == "223.5.5.5"]
        self.assertIn("www.example.org", resolved)
        self.assertEqual(self._page_requests()[-1].url.host, "93.184.216.35")

    async def test_redirect_loop_gives_up(self):
        self.pages[("news.example.com", "/a")] = httpx.Response(302, headers={"location": "/a"})

        self.assertIsNone(await direct_read.read_direct("https://news.example.com/a", "d"))
        self.assertEqual(len(self._page_requests()), direct_read.DIRECT_READ_MAX_REDIRECTS + 1)

    async def test_non_default_port_and_credentials_are_skipped(self):
        self.assertIsNone(await direct_read.read_direct("https://news.example.com:8443/a", "d"))
        self.assertIsNone(await direct_read.read_direct("https://u:p@news.example.com/a", "d"))
        self.assertEqual(self.requests, [])

    async def test_non_html_error_status_and_short_text_fall_back(self):
        self.pages[("news.example.com", "/pdf")] = httpx.Response(
            200, content=b"%PDF", headers={"content-type": "application/pdf"}
        )
        self.pages[("news.example.com", "/blocked")] = httpx.Response(
            403, text="<html>Enable JavaScript</html>", headers={"content-type": "text/html"}
        )
        self._html("news.example.com", "/short", "<html><body><p>Enable JavaScript and cookies</p></body></html>")

        for path in ("/pdf", "/blocked", "/short"):
            with self.subTest(path=path):
                self.assertIsNone(await direct_read.read_direct(f"https://news.example.com{path}", "d"))

    async def test_oversized_body_is_abandoned(self):
        self._html("news.example.com", "/big")
        with patch("direct_read.DIRECT_READ_MAX_BYTES", 1024):
            self.assertIsNone(await direct_read.read_direct("https://news.example.com/big", "d"))

    async def test_disabled_flag_makes_no_requests(self):
        with patch("direct_read.DIRECT_READ_ENABLED", False):
            self.assertIsNone(await direct_read.read_direct("https://news.example.com/a", "d"))
        self.assertEqual(self.requests, [])

    async def test_read_url_uses_direct_result_and_skips_providers(self):
        self._html("news.example.com", "/a")
        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])), patch(
            "main._read_with_tavily", new=AsyncMock()
        ) as tavily, patch("main._fetch_jina", new=AsyncMock()) as jina:
            response = await main.read_url("https://news.example.com/a")

        self.assertEqual(response.provider, "direct")
        self.assertEqual(response.attempts, 1)
        self.assertEqual(response.title, "测试文章标题")
        tavily.assert_not_awaited()
        jina.assert_not_awaited()

    async def test_full_mode_skips_direct_read(self):
        self._html("news.example.com", "/a")
        with patch("main._tavily_pool", main.TavilyKeyPool(["tvly-test"])), patch(
            "main._read_with_tavily", new=AsyncMock(return_value=("# 整页\n导航 正文", "整页"))
        ) as tavily, patch("main._fetch_jina", side_effect=ReaderServiceTests._hang):
            response = await main.read_url("https://news.example.com/a", full=True)

        self.assertEqual(response.provider, "tavily")
        tavily.assert_awaited_once()
        self.assertEqual(self.requests, [])

    def test_container_ships_direct_read_module(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text()
        self.assertIn("direct_read.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
