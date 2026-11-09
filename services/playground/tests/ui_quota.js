// 仅针对额度为 3 次、生图 1 次的本地测试页面运行，不调用真实供应商。
async (page) => {
  if (new URL(page.url()).port !== "18101") throw new Error("只允许在本地测试页面验证额度用尽")
  const checks = []
  const check = (condition, name) => { if (!condition) throw new Error(name); checks.push(name) }
  const category = name => page.locator(".tool-navigation").getByRole("button", {name, exact: true}).click()
  const run = async () => {
    const pending = page.waitForResponse(response => response.url().endsWith("/api/run") && response.request().method() === "POST")
    await page.locator("#run-button").click()
    const data = await (await pending).json()
    await page.waitForFunction(() => !document.querySelector("#run-button span").textContent.includes("正在运行"))
    return data
  }
  await page.reload()
  await page.getByRole("button", {name: "刷新剩余次数", exact: true}).waitFor()
  await page.waitForFunction(() => document.querySelector(".quota-counters"))
  check((await page.locator(".quota-counter").first().innerText()).includes("3 / 3"), "页面显示个人剩余次数")
  await page.getByRole("button", {name: "刷新剩余次数", exact: true}).click()
  check((await page.locator(".quota-counter").first().innerText()).includes("3 / 3"), "查看额度不消耗次数")
  await category("图像生成")
  await page.getByRole("button", {name: "海岸灯塔", exact: true}).click()
  const image = await run()
  check(image.ok && image.quota.requests.remaining === 2 && image.quota.images.remaining === 0, "生图同时扣总次数与生图次数")
  check(!await page.locator("#run-button").isEnabled(), "生图用完后禁用生图按钮")
  await category("联网搜索")
  check(await page.locator("#run-button").isEnabled(), "生图用完仍可使用其他工具")
  await page.getByRole("button", {name: "探索 Python", exact: true}).click()
  const search = await run()
  check(search.quota.requests.remaining === 1, "搜索后计数实时减少")
  const pendingRead = page.waitForResponse(response => response.url().endsWith("/api/run") && response.request().method() === "POST")
  await page.getByRole("button", {name: "读取全文", exact: true}).first().click()
  const read = await (await pendingRead).json()
  await page.locator("#reader-content .markdown").waitFor()
  check(read.ok && read.quota.requests.remaining === 0, "读取全文独立计数，保留正文结果")
  check(!await page.locator("#run-button").isEnabled(), "总次数用完后禁用运行按钮")
  check(await page.locator(".read-button:enabled").count() === 0, "用完后不能继续从搜索列表发起读取")
  await page.reload()
  await page.waitForFunction(() => document.querySelector(".quota-exhausted"))
  check((await page.locator(".quota-counter").first().innerText()).includes("0 / 3"), "刷新页面后仍为零次")
  const direct = await page.request.post("http://127.0.0.1:18101/api/run", {data: {tool: "search_web", arguments: {query: "绕过前端测试"}}})
  check(direct.status() === 429, "直接请求接口也无法绕过计数")
  check((await direct.json()).quota.requests.used === 3, "被拒绝的请求不继续计数")
  await page.setViewportSize({width: 375, height: 900})
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "375px 下计数卡片不撑宽页面")
  return {passed: checks.length, checks}
}
