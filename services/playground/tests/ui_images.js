// 验证实际 REST 相对地址、原图加载和重试，必须连接本地测试网关。
async (page) => {
  if (new URL(page.url()).port !== "18101") throw new Error("只允许本地测试页面")
  await page.goto("http://127.0.0.1:18101/#tool=generate_image")
  await page.reload()
  await page.locator("#example-status").waitFor({ state: "visible" })
  const checks = []
  const check = (condition, name) => {
    if (!condition) throw new Error(name)
    checks.push(name)
  }
  let generations = 0
  page.on("request", request => {
    if (request.url().endsWith("/api/run")) generations++
  })
  const before = await (await page.request.get("http://127.0.0.1:18101/api/quota")).json()
  await page.route("**/api/images/*", route => route.fulfill({ status: 503, body: "test failure" }), { times: 1 })
  await page.locator("#run-button").click()
  await page.getByText("图片已生成，但预览加载失败。可打开原图或重新加载预览，无需重新生成。", { exact: true }).waitFor()
  check(await page.locator(".result-image").count() === 1, "相对地址创建了预览图片")
  const link = page.getByRole("link", { name: "打开图片 / 保存原图", exact: true })
  check((await link.getAttribute("href")).startsWith("http://127.0.0.1:18101/api/images/"), "相对地址通过同源原图入口展示")
  await page.getByRole("button", { name: "重新加载预览", exact: true }).click()
  await page.waitForFunction(() => document.querySelector(".result-image")?.naturalWidth > 0)
  check(await page.locator("#result-main .notice").count() === 0, "重试成功后清除失败提示")
  const image = await page.request.get(await link.getAttribute("href"))
  check(image.ok() && image.headers()["content-type"] === "image/png", "打开原图返回可识别的图片")
  await page.getByRole("button", { name: "重新加载预览", exact: true }).click()
  await page.waitForFunction(() => document.querySelector(".result-image")?.naturalWidth > 0)
  const after = await (await page.request.get("http://127.0.0.1:18101/api/quota")).json()
  check(generations === 1, "预览重试和打开原图均不重复生图")
  check(after.images.used === before.images.used + 1 && after.requests.used === before.requests.used + 1, "多次读取只扣除一次生图调用")
  return { passed: checks.length, checks }
}
