// 使用与线上相同的 FastMCP 包装结果，核对热榜来源、名次、链接和北京时间。
async (page) => {
  if (new URL(page.url()).port !== "18101") throw new Error("只允许本地测试页面")
  const session = await page.context().newCDPSession(page)
  await session.send("Emulation.setTimezoneOverride", { timezoneId: "America/New_York" })
  const checks = []
  const check = (condition, name) => {
    if (!condition) throw new Error(name)
    checks.push(name)
  }
  try {
    await page.goto("http://127.0.0.1:18101/?check=trends#tool=get_latest_news")
    await page.locator("#result-main .example-notice").waitFor({ state: "visible" })
    const pending = page.waitForResponse(response => response.url().endsWith("/api/run"))
    await page.locator("#run-button").click()
    const response = await pending
    const body = await response.json()
    check(response.ok() && body.ok, "线上形式的 JSON 字符串包装能正确解析")
    await page.waitForFunction(() => !document.querySelector("#run-button").disabled)
    check(body.arguments.include_url === true, "真实热榜请求包含新闻来源链接")
    check(await page.locator("#result-main .search-result").count() === 1, "热榜显示为新闻卡片")
    check((await page.locator("#result-main").textContent()).includes("微博 · 第 1 名"), "显示平台中文名称和榜单名次")
    check((await page.locator("#result-main").textContent()).includes("11/10 01:30（北京时间）"), "浏览器位于纽约时仍正确显示北京时间")
    check(await page.locator("#result-main .search-title").getAttribute("href") === "https://example.com/news", "标题可打开真实来源地址")
    check(await page.locator("#result-main .example-notice").count() === 0, "真实热榜不会被标为示例")
    return { passed: checks.length, checks }
  } finally {
    await session.send("Emulation.setTimezoneOverride", { timezoneId: "" })
    await session.detach()
  }
}
