// 使用本地测试网关核对所有操作，不访问真实供应商。
async (page) => {
  const base = "http://127.0.0.1:18101/"
  if (new URL(page.url()).port !== "18101")
    throw new Error("默认值回归只允许在本地测试页面运行")
  const tools = (await (await page.request.get(`${base}api/catalog`)).json()).tools
  const checks = []
  const check = (condition, message) => {
    if (!condition) throw new Error(message)
    checks.push(message)
  }
  let calls = 0
  page.on("request", (request) => {
    if (request.url().endsWith("/api/run")) calls++
  })
  const open = async (id) => {
    await page.goto(`${base}?check=${id}#tool=${id}`)
    await page.locator("#example-status").waitFor({ state: "visible" })
  }
  const run = async () => {
    const pending = page.waitForResponse((response) =>
      response.url().endsWith("/api/run") && response.request().method() === "POST",
    )
    await page.locator("#run-button").click()
    const response = await pending
    const data = await response.json()
    check(response.ok() && data.ok, `默认参数实际调用通过：${data.tool}`)
    await page.waitForFunction(() => !document.querySelector("#run-button").disabled)
    return data.arguments
  }
  for (const tool of tools) {
    await open(tool.id)
    const actual = await page.locator("#tool-form").evaluate((form) => {
      const result = {}
      const data = new FormData(form)
      for (const input of form.querySelectorAll("input[name], textarea[name]")) {
        if (input.type === "checkbox") {
          if (input.name === "modes") result[input.name] = data.getAll(input.name)
          else result[input.name] = input.checked
        } else result[input.name] = input.value
      }
      return result
    })
    for (const field of tool.fields) {
      const expected = tool.examples[0].arguments[field.name]
      check(
        Array.isArray(expected)
          ? JSON.stringify(actual[field.name]) === JSON.stringify(expected)
          : String(actual[field.name]) === String(expected),
        `${tool.id} 初始字段有明确值：${field.name}`,
      )
    }
    check(await page.locator("#tool-form").evaluate((form) => form.checkValidity()), `${tool.id} 初次打开即可提交`)
    check(await page.locator('#examples [aria-pressed="true"]').count() === 1, `${tool.id} 标明当前示例`)
    const before = calls
    for (const example of tool.examples) {
      await page.getByRole("button", { name: example.label, exact: true }).click()
      check(await page.locator("#tool-form").evaluate((form) => form.checkValidity()), `${tool.id} 示例可提交：${example.label}`)
    }
    check(calls === before, `${tool.id} 选择示例不自动调用`)
    await run()
  }
  await open("search_web")
  await page.getByRole("radio", { name: "10", exact: true }).click()
  await page.getByRole("combobox", { name: "时间范围", exact: true }).click()
  await page.getByRole("option", { name: "最近一周", exact: true }).click()
  check(await page.locator('#examples [aria-pressed="true"]').count() === 0, "修改筛选后取消示例高亮")
  await page.getByRole("button", { name: "城市漫游", exact: true }).click()
  check(await page.getByRole("radio", { name: "5", exact: true }).getAttribute("aria-checked") === "true", "选择示例恢复五条结果并高亮")
  const search = await run()
  check(search.count === 5 && search.recency_days == null, "选择示例清除旧的时间范围")
  await page.getByLabel("你想了解什么？", { exact: true }).fill("")
  await page.locator(".tool-navigation").getByRole("button", { name: "天气预报", exact: true }).click()
  await page.locator(".tool-navigation").getByRole("button", { name: "联网搜索", exact: true }).click()
  check(await page.getByLabel("你想了解什么？", { exact: true }).inputValue() === "", "切换工具保留主动清空的输入")
  await page.getByRole("button", { name: "探索 Python", exact: true }).click()
  check(await page.getByLabel("你想了解什么？", { exact: true }).inputValue() !== "", "重新选择示例恢复完整参数")
  await open("generate_image")
  await page.getByRole("radio", { name: "9:16", exact: true }).click()
  await page.getByRole("radio", { name: "高质量", exact: true }).click()
  await page.getByRole("button", { name: "纸上花园", exact: true }).click()
  const image = await run()
  check(image.aspect_ratio === "1:1" && image.quality === "standard", "纸艺示例恢复方图和标准质量")
  await page.getByRole("button", { name: "海岸灯塔", exact: true }).click()
  check(await page.getByRole("radio", { name: "16:9", exact: true }).getAttribute("aria-checked") === "true", "灯塔示例恢复横向构图")
  await open("get_weather")
  await page.getByLabel("指定日期（可选）", { exact: true }).fill("2026-11-10")
  await page.getByRole("button", { name: "上海天气", exact: true }).click()
  const weather = await run()
  check(!weather.requested_date, "天气示例恢复自动查询近期预报")
  await open("route_compare")
  await page.getByLabel("到达城市（跨城时填写）", { exact: true }).fill("杭州")
  await page.locator('input[name="modes"][value="driving"]').uncheck()
  await page.locator('input[name="modes"][value="walking"]').check()
  await page.getByRole("button", { name: "虹桥到外滩", exact: true }).click()
  const route = await run()
  check(!route.destination_city && JSON.stringify(route.modes) === '["driving","transit"]', "路线示例清除跨城信息并恢复驾车和公交")
  await open("search_trains")
  const defaultDate = await page.getByLabel("出发日期", { exact: true }).inputValue()
  await page.getByLabel("出发日期", { exact: true }).fill("2030-01-01")
  await page.getByRole("radio", { name: "全部车型", exact: true }).click()
  await page.getByRole("button", { name: "杭州 → 上海", exact: true }).click()
  const train = await run()
  check(train.train_category === "high_speed" && train.departure_date === defaultDate, "火车示例恢复明天和高铁动车")
  await open("read_page")
  await page.getByRole("switch", { name: "保留整页内容", exact: true }).check()
  await page.getByRole("button", { name: "FastAPI 文档", exact: true }).click()
  check((await run()).full_page === false, "网页示例恢复默认正文提取")
  await page.reload()
  check(await page.locator("#tool-form").evaluate((form) => form.checkValidity()), "刷新后仍有完整默认参数")
  return { passed: checks.length, checks }
}
