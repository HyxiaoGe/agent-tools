// 固定示例与真实结果相互独立；只在连接测试网关的页面运行。
async (page) => {
  const base = "http://127.0.0.1:18101/"
  if (new URL(page.url()).port !== "18101") throw new Error("只允许本地测试页面")
  const catalog = await (await page.request.get(`${base}api/catalog`)).json()
  const before = await (await page.request.get(`${base}api/quota`)).json()
  const checks = []
  const check = (condition, name) => {
    if (!condition) throw new Error(name)
    checks.push(name)
  }
  let calls = 0
  page.on("request", request => {
    if (request.url().endsWith("/api/run")) calls++
  })
  const open = async id => {
    await page.goto(`${base}?sample=${id}#tool=${id}`)
    await page.locator("#result-main .example-notice").waitFor({ state: "visible" })
  }
  const mode = example => page.locator("#result-views").getByRole("button", { name: example ? "示例预览" : "真实结果", exact: true })
  for (const tool of catalog.tools) {
    await open(tool.id)
    check((await page.locator("#result-meta").textContent()).includes("示例数据"), `${tool.id} 默认标明示例数据`)
    check((await page.locator("#result-main").textContent()).includes("不随左侧参数变化"), `${tool.id} 说明固定样例边界`)
    check(await mode(true).getAttribute("aria-pressed") === "true" && !await mode(false).isEnabled(), `${tool.id} 不把示例当成真实结果`)
    check((await page.locator("#result-main").textContent()).length > 150, `${tool.id} 有实际内容`)
    if (tool.id === "generate_image") {
      await page.waitForFunction(() => document.querySelector("#result-main .result-image")?.naturalWidth > 1000)
      check((await page.locator(".result-image").getAttribute("src")).includes("/assets/examples/lighthouse.jpeg"), "图片示例来自随服务打包的资源")
      const originalAlt = await page.locator(".result-image").getAttribute("alt")
      await page.getByLabel("描述你想看到的画面", { exact: true }).fill("修改参数不伪造新的示例图片")
      check(await page.locator(".result-image").getAttribute("alt") === originalAlt, "修改参数不会伪称固定图片是新生成的")
    }
  }
  check(calls === 0, "遍历全部示例没有发起工具调用")
  check(JSON.stringify(before) === JSON.stringify(await (await page.request.get(`${base}api/quota`)).json()), "浏览全部示例和图片未扣次数")
  await open("search_web")
  await page.getByRole("button", { name: "查看示例正文", exact: true }).first().click()
  check(await page.locator("#reader-content .example-notice").isVisible(), "搜索衔接正文仍标明示例")
  check(await page.locator("#reader-content table").count() === 1, "示例正文支持 Markdown 表格")
  check(calls === 0, "示例正文读取不调用真实工具")
  await page.getByRole("button", { name: "关闭正文，返回参数", exact: true }).click()
  const response = page.waitForResponse(item => item.url().endsWith("/api/run"))
  await page.locator("#run-button").click()
  await response
  await page.waitForFunction(() => !document.querySelector("#run-button").disabled)
  check(await mode(false).getAttribute("aria-pressed") === "true", "真实运行完成后自动展示真实结果")
  check(await page.locator("#result-main .example-notice").count() === 0, "真实结果没有示例标识")
  const realText = await page.locator("#result-main").textContent()
  await mode(true).click()
  check(await page.locator("#result-main .example-notice").isVisible(), "真实运行后仍可查看示例")
  await mode(false).click()
  check(await page.locator("#result-main").textContent() === realText, "切换回来保留原真实结果")
  check(calls === 1, "切换示例与真实结果不重复调用")
  await mode(true).click()
  await page.route("**/api/quota", async route => {
    const quota = await (await route.fetch()).json()
    quota.can_run = false
    quota.can_generate = false
    quota.requests.remaining = 0
    await route.fulfill({ json: quota })
  })
  await page.getByRole("button", { name: "刷新剩余次数", exact: true }).click()
  await page.waitForFunction(() => document.querySelector("#run-button").disabled)
  const read = page.getByRole("button", { name: "查看示例正文", exact: true }).first()
  check(await read.isEnabled(), "额度用完仍可浏览示例正文")
  await read.click()
  check(calls === 1, "额度用完查看示例也不调用工具")
  await page.getByRole("button", { name: "关闭正文，返回参数", exact: true }).click()
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.locator(".tool-navigation").getByRole("button", { name: "图像生成", exact: true }).click()
    await page.waitForFunction(() => document.querySelector(".result-image")?.naturalWidth > 1000)
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 示例图片与切换按钮不溢出`)
  }
  await page.unroute("**/api/quota")
  await page.route("**/api/catalog", async route => {
    const body = await (await route.fetch()).json()
    body.configured = false
    await route.fulfill({ json: body })
  })
  await open("generate_image")
  await page.waitForFunction(() => document.querySelector(".result-image")?.naturalWidth > 1000)
  check(!await page.locator("#run-button").isEnabled(), "未配置密钥时真实运行不可用")
  check(await page.locator(".result-image").isVisible(), "未配置密钥时图片示例仍可见")
  await page.unroute("**/api/catalog")
  return { passed: checks.length, checks }
}
