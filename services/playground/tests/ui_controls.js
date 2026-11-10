// 使用 Playwright CLI 的 run-code --filename 运行；目标必须是连接本地测试网关的 18101 页面。
async (page) => {
  if (new URL(page.url()).port !== "18101")
    throw new Error("组件回归只允许在本地测试页面运行")
  const checks = []
  const check = (condition, name) => {
    if (!condition) throw new Error(name)
    checks.push(name)
  }
  const category = (name) =>
    page
      .locator(".tool-navigation")
      .getByRole("button", { name, exact: true })
      .click()
  const run = async () => {
    const pending = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/run") &&
        response.request().method() === "POST",
    )
    await page.getByRole("button", { name: /运行工具/ }).click()
    const response = await pending
    const result = await response.json()
    if (!response.ok() || !result.ok)
      throw new Error(`测试工具调用失败：${result.error?.code}`)
    await page.waitForFunction(
      () => !document.querySelector("#run-button").disabled,
    )
    return result
  }
  await page.reload()
  await category("联网搜索")
  check(
    (await page.locator("select").count()) === 0,
    "选择器不再使用系统下拉外观",
  )
  const combo = page.getByRole("combobox", { name: "时间范围", exact: true })
  await combo.click()
  await combo.press("ArrowDown")
  await combo.press("Escape")
  check(
    (await combo.textContent()).includes("不限时间"),
    "Esc 取消菜单，原值不变",
  )
  check(
    (await page.getByRole("listbox").count()) === 0,
    "关闭后移除菜单与活动选项",
  )
  await combo.click()
  await combo.press("ArrowDown")
  await combo.press("Tab")
  check(
    (await combo.textContent()).includes("最近一周"),
    "Tab 提交高亮项并正常移出焦点",
  )
  await page.getByRole("radio", { name: "5", exact: true }).press("End")
  check(
    (await page
      .getByRole("radio", { name: "10", exact: true })
      .getAttribute("aria-checked")) === "true",
    "分段选择支持 End 键",
  )
  await page.getByRole("radio", { name: "10", exact: true }).press("Home")
  check(
    (await page
      .getByRole("radio", { name: "3", exact: true })
      .getAttribute("aria-checked")) === "true",
    "分段选择支持 Home 键与焦点移动",
  )
  await page.getByLabel("你想了解什么？", { exact: true }).fill("组件回归测试")
  const search = await run()
  check(
    search.arguments.count === 3 && search.arguments.recency_days === 7,
    "自定义选择器提交的搜索参数正确",
  )
  await combo.click()
  await category("图像生成")
  check(
    (await page.getByRole("listbox").count()) === 0,
    "切换工具清理尚未关闭的菜单",
  )
  await page.getByRole("radio", { name: "16:9", exact: true }).click()
  await page
    .getByRole("radio", { name: "16:9", exact: true })
    .press("ArrowRight")
  await page.getByRole("radio", { name: "高质量", exact: true }).click()
  await page
    .getByLabel("描述你想看到的画面", { exact: true })
    .fill("仅用于本地组件回归的画面")
  const image = await run()
  check(
    image.arguments.aspect_ratio === "9:16" &&
      image.arguments.quality === "high",
    "比例卡片与质量卡片提交正确值",
  )
  await category("联网搜索")
  check(
    (await page
      .getByRole("radio", { name: "3", exact: true })
      .getAttribute("aria-checked")) === "true",
    "切换工具保留分段选择值",
  )
  check(
    (await combo.textContent()).includes("最近一周"),
    "切换工具保留下拉菜单值",
  )
  await category("航班与铁路")
  await page.getByRole("button", { name: "北京 → 上海", exact: true }).click()
  const date = page.getByLabel("出发日期", { exact: true })
  check(
    (await page.locator('input[type="date"]').count()) === 0,
    "日期控件使用自定义日历",
  )
  let leapYear = Number((await date.inputValue()).slice(0, 4)) + 2
  while (leapYear % 4 !== 0 || (leapYear % 100 === 0 && leapYear % 400 !== 0))
    leapYear++
  await date.fill(`${leapYear - 1}-01-31`)
  await page.getByRole("button", { name: "选择出发日期", exact: true }).click()
  await page.locator(".calendar-day:focus").press("PageDown")
  check(
    (await page.locator(".calendar-day:focus").getAttribute("data-date")) ===
      `${leapYear - 1}-02-28`,
    "跨月移动正确处理月末",
  )
  await page.locator(".calendar-day:focus").press("Shift+PageDown")
  await page.locator(".calendar-day:focus").press("ArrowRight")
  check(
    (await page.locator(".calendar-day:focus").getAttribute("data-date")) ===
      `${leapYear}-02-29`,
    "日历正确处理闰年与跨年键盘操作",
  )
  await page.locator(".calendar-day:focus").press("Enter")
  check(
    (await date.inputValue()) === `${leapYear}-02-29`,
    "选择日期后回填标准格式",
  )
  check(
    await page
      .getByRole("button", { name: "选择出发日期", exact: true })
      .evaluate((node) => node === document.activeElement),
    "选择完成后焦点返回日历按钮",
  )
  const flight = await run()
  check(
    flight.arguments.departure_date === `${leapYear}-02-29`,
    "自定义日历提交的日期正确",
  )
  await date.fill("2026-02-31")
  check(
    await date.evaluate((node) => !node.checkValidity()),
    "手工输入非法日期时阻止提交",
  )
  await category("天气预报")
  await page.getByRole("button", { name: "杭州天气", exact: true }).click()
  await page.getByRole("button", { name: "选择指定日期", exact: true }).click()
  await page.getByRole("button", { name: "今天", exact: true }).click()
  await page.getByRole("button", { name: "选择指定日期", exact: true }).click()
  await page.getByRole("button", { name: "清除", exact: true }).click()
  const weather = await run()
  check(!("requested_date" in weather.arguments), "可选日期清除后不提交空日期")
  await category("网页读取")
  await page
    .getByLabel("网页地址", { exact: true })
    .fill("https://example.com/article")
  await page.getByRole("switch").check()
  const read = await run()
  check(read.arguments.full_page === true, "整页读取开关保留布尔值语义")
  await category("地图与地点")
  await page.getByRole("button", { name: "比较路线", exact: true }).click()
  await page.getByRole("button", { name: "虹桥到外滩", exact: true }).click()
  await page.getByLabel("步行", { exact: true }).check()
  await page.getByLabel("骑行", { exact: true }).click()
  check(
    (await page.locator(".multi-options input:checked").count()) === 3,
    "路线卡片限制最多三种出行方式",
  )
  const route = await run()
  check(
    route.arguments.modes.length === 3 &&
      !route.arguments.modes.includes("bicycling"),
    "路线卡片提交与选中状态一致",
  )
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await category("联网搜索")
    check(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `${width}px 页面无横向溢出`,
    )
    await combo.click()
    check(
      await page.getByRole("listbox").evaluate((node) => {
        const r = node.getBoundingClientRect()
        return (
          r.left >= 0 &&
          r.right <= innerWidth &&
          r.top >= 0 &&
          r.bottom <= innerHeight
        )
      }),
      `${width}px 下拉菜单完整位于视口内`,
    )
    await combo.press("Escape")
    await category("航班与铁路")
    await date.fill(`${leapYear}-02-29`)
    await page
      .getByRole("button", { name: "选择出发日期", exact: true })
      .click()
    check(
      await page.getByRole("dialog").evaluate((node) => {
        const r = node.getBoundingClientRect()
        return (
          r.left >= 0 &&
          r.right <= innerWidth &&
          r.top >= 0 &&
          r.bottom <= innerHeight
        )
      }),
      `${width}px 日历完整位于视口内`,
    )
    await page.locator(".calendar-day:focus").press("Escape")
  }
  return { passed: checks.length, checks }
}
