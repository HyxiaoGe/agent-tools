import {
  closeControls,
  createChoices,
  createDatePicker,
  createSelect,
} from "./components.js";

const $ = (selector) => document.querySelector(selector);
const state = {
  catalog: null,
  tool: "search_web",
  values: new Map(),
  results: new Map(),
  examples: {},
  previewTools: new Set(),
  pending: null,
  reader: null,
  quota: null,
};
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const icon = (name, className = "icon") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
};
const toolById = (id) => state.catalog.tools.find((tool) => tool.id === id);
const categoryById = (id) =>
  state.catalog.categories.find((category) => category.id === id);
const safeUrl = (value) => {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
};
const link = (text, url, className = "text-link") => {
  const safe = safeUrl(url);
  if (!safe) return el("span", className, text);
  const node = el("a", className, text);
  node.href = safe;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
};
const button = (text, className, action, iconName) => {
  const node = el("button", className);
  node.type = "button";
  if (iconName) node.append(icon(iconName));
  node.append(document.createTextNode(text));
  node.addEventListener("click", action);
  return node;
};
const pretty = (value) => JSON.stringify(value, null, 2);
const duration = (ms) =>
  ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
const minutes = (value) =>
  Number.isFinite(value)
    ? `${Math.floor(value / 60) ? `${Math.floor(value / 60)} 小时 ` : ""}${value % 60} 分钟`
    : "用时未知";
const observedAt = (value) => {
  if (!value || Number.isNaN(new Date(value).valueOf())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
};
let toastTimer;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 2400);
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    toast("复制不可用，请从调用详情中手动选取内容");
  }
}

function buildNavigation() {
  const navigation = $("#tool-navigation");
  for (const category of state.catalog.categories) {
    const node = button(
      category.title,
      "nav-tool",
      () => selectCategory(category.id),
      category.icon,
    );
    node.dataset.category = category.id;
    node.setAttribute("aria-pressed", "false");
    node.append(icon("arrow", "icon nav-arrow"));
    navigation.append(node);
  }
}
function selectCategory(id) {
  const current = toolById(state.tool);
  const next =
    current.category === id
      ? current
      : state.catalog.tools.find((tool) => tool.category === id);
  selectTool(next.id);
}
function rememberForm() {
  if (!$("#form-fields").childElementCount) return;
  state.values.set(state.tool, formArguments(true));
}
function selectTool(id) {
  rememberForm();
  state.tool = id;
  history.replaceState(
    null,
    "",
    `${location.pathname}${location.search}#tool=${encodeURIComponent(id)}`,
  );
  const tool = toolById(id);
  const category = categoryById(tool.category);
  $("#active-tool-icon").replaceChildren(icon(category.icon));
  $("#active-tool-title").textContent = category.title;
  $("#active-tool-en").textContent = category.en.toUpperCase();
  $("#active-tool-description").textContent = category.description;
  $("#active-providers").replaceChildren(
    ...category.providers.map((name) => el("span", "", name)),
  );
  document
    .querySelectorAll(".nav-tool")
    .forEach((node) =>
      node.setAttribute(
        "aria-pressed",
        String(node.dataset.category === category.id),
      ),
    );
  const siblings = state.catalog.tools.filter(
    (entry) => entry.category === tool.category,
  );
  $("#tool-tabs").replaceChildren(
    ...(siblings.length > 1
      ? siblings.map((entry) => {
          const node = button(entry.title, "", () => selectTool(entry.id));
          node.setAttribute("aria-pressed", String(entry.id === id));
          return node;
        })
      : []),
  );
  buildForm(tool);
  renderQuota();
  $("#form-error").hidden = true;
  renderResult();
  renderReader();
  updateRunButton();
}
function buildForm(tool) {
  closeControls();
  const target = $("#form-fields");
  target.replaceChildren();
  const saved =
    state.values.get(tool.id) || tool.examples[0]?.arguments || tool.defaults;
  for (const field of tool.fields) {
    const value = saved[field.name] ?? field.default;
    const wrapper = el(
      field.type === "multiselect" ? "fieldset" : "div",
      `field${field.type === "checkbox" ? " checkbox-field" : ""}`,
    );
    const label = el(
      field.type === "multiselect" ? "legend" : "label",
      "",
      field.label,
    );
    const id = `field-${field.name}`;
    label.id = `label-${field.name}`;
    if (field.type !== "multiselect") label.htmlFor = id;
    wrapper.append(label);
    if (field.type === "multiselect") {
      const options = el("div", "multi-options");
      field.options.forEach((option) => {
        const item = el("label");
        const input = el("input");
        input.type = "checkbox";
        input.name = field.name;
        input.value = option.value;
        input.checked = Array.isArray(value) && value.includes(option.value);
        input.addEventListener("change", () => {
          if (options.querySelectorAll("input:checked").length > 3) {
            input.checked = false;
            toast("最多比较三种出行方式");
          }
        });
        item.append(input, icon("check"), el("span", "", option.label));
        options.append(item);
      });
      wrapper.append(options);
      wrapper.append(el("p", "field-help", "最多选择 3 种方式"));
    } else if (field.type === "select") {
      const choices = [
        "count",
        "limit",
        "aspect_ratio",
        "quality",
        "train_category",
      ].includes(field.name);
      wrapper.append(
        choices
          ? createChoices(field, value, label.id)
          : createSelect(field, value, label.id),
      );
      if (choices) label.removeAttribute("for");
    } else if (field.type === "date") {
      wrapper.append(createDatePicker(field, value, label.id));
      if (field.help) wrapper.append(el("p", "field-help", field.help));
    } else {
      const input = el(field.type === "textarea" ? "textarea" : "input");
      input.id = id;
      input.name = field.name;
      if (field.type !== "textarea") {
        input.type = field.type;
      }
      if (field.type === "checkbox") {
        input.checked = Boolean(value);
        input.setAttribute("role", "switch");
        const copy = el("span", "switch-copy");
        const title = el("span", "switch-title", field.label);
        title.id = `switch-title-${field.name}`;
        input.setAttribute("aria-labelledby", title.id);
        copy.append(title);
        if (field.help) {
          const help = el("span", "switch-help", field.help);
          help.id = `switch-help-${field.name}`;
          input.setAttribute("aria-describedby", help.id);
          copy.append(help);
        }
        label.replaceChildren(copy, input);
      } else {
        input.value = value;
        input.required = Boolean(field.required);
        if (field.placeholder) input.placeholder = field.placeholder;
        if (field.type === "textarea")
          input.maxLength = tool.id === "generate_image" ? 3000 : 1000;
        if (field.type === "text" || field.type === "url")
          input.maxLength = field.type === "url" ? 4096 : 200;
        wrapper.append(input);
      }
      if (field.help && field.type !== "checkbox")
        wrapper.append(el("p", "field-help", field.help));
    }
    target.append(wrapper);
  }
  if (tool.id === "search_web") {
    const row = el("div", "field-row");
    const fields = [...target.children];
    row.append(fields[1], fields[2]);
    target.append(row);
  }
  $("#examples").replaceChildren(
    ...tool.examples.map((example) =>
      button(
        example.label,
        "",
        () => {
          state.values.set(tool.id, example.arguments);
          buildForm(tool);
          $("#form-error").hidden = true;
          const first = $(
            "#form-fields input:not([type=hidden]), #form-fields textarea, #form-fields button",
          );
          first?.focus({ preventScroll: true });
        },
        "arrow",
      ),
    ),
  );
  $(".example-section").hidden = !tool.examples.length;
  updateExampleSelection();
}
function updateExampleSelection() {
  const tool = toolById(state.tool);
  const values = formArguments(true);
  let selected;
  [...$("#examples").children].forEach((node, index) => {
    const example = tool.examples[index];
    const matches = tool.fields.every(({ name }) =>
      Array.isArray(values[name])
        ? JSON.stringify(values[name]) ===
          JSON.stringify(example.arguments[name])
        : String(values[name]) === String(example.arguments[name]),
    );
    node.setAttribute("aria-pressed", String(matches));
    if (matches) selected = example.label;
  });
  $("#example-status").textContent = selected
    ? `已载入「${selected}」· 可直接运行`
    : "自定义参数 · 点击示例可恢复配套筛选";
}
function formArguments(includeEmpty = false) {
  const tool = toolById(state.tool);
  const data = new FormData($("#tool-form"));
  const result = {};
  for (const field of tool.fields) {
    let value;
    if (field.type === "checkbox") value = data.has(field.name);
    else if (field.type === "multiselect") value = data.getAll(field.name);
    else value = String(data.get(field.name) ?? "").trim();
    if (value === "" && !includeEmpty) continue;
    if (value !== "" && ["count", "recency_days", "limit"].includes(field.name))
      value = Number(value);
    result[field.name] = value;
  }
  return result;
}
let quotaVersion = 0;
let quotaResetTimer;
function renderQuota() {
  const panel = $("#quota-panel");
  panel.hidden = !state.catalog?.configured;
  if (panel.hidden) return;
  const header = el("div", "quota-heading");
  header.append(
    el("strong", "", "今日试用"),
    button("刷新", "quota-refresh", refreshQuota),
  );
  header.lastChild.setAttribute("aria-label", "刷新剩余次数");
  panel.replaceChildren(header);
  clearTimeout(quotaResetTimer);
  const quota = state.quota;
  if (!quota) {
    panel.append(el("p", "quota-note", "暂时无法读取剩余次数，请刷新后再试。"));
  } else {
    const counters = el("div", "quota-counters");
    for (const [key, label] of [
      ["requests", "试用剩余"],
      ["images", "生图剩余"],
    ]) {
      const item = el("div", "quota-counter");
      item.append(
        el("span", "", label),
        el("strong", "", `${quota[key].remaining} / ${quota[key].limit}`),
      );
      counters.append(item);
    }
    panel.append(
      counters,
      el(
        "p",
        "quota-site",
        `全站今日剩余 ${quota.site_requests.remaining} 次 · 生图 ${quota.site_images.remaining} 次`,
      ),
    );
    panel.append(el("p", "quota-note", "按 IP 计数 · 北京时间零点恢复"));
    if (!quota.can_run)
      panel.append(
        el("p", "quota-exhausted", "今日试用次数已用完，明天再来看看。"),
      );
    else if (state.tool === "generate_image" && !quota.can_generate)
      panel.append(
        el("p", "quota-exhausted", "今日生图次数已用完，其他工具仍可试用。"),
      );
    quotaResetTimer = setTimeout(
      refreshQuota,
      Math.max(1000, new Date(quota.resets_at).valueOf() - Date.now() + 150),
    );
  }
}
async function refreshQuota() {
  if (!state.catalog?.configured) return;
  const version = ++quotaVersion;
  try {
    const response = await fetch("/api/quota", {
      signal: AbortSignal.timeout(5000),
    });
    const quota = await response.json();
    if (!response.ok || typeof quota.can_run !== "boolean")
      throw new Error("quota_unavailable");
    if (version !== quotaVersion) return;
    state.quota = quota;
  } catch {
    if (version !== quotaVersion) return;
    state.quota = null;
  }
  renderQuota();
  updateRunButton();
}
function updateRunButton() {
  const pending = Boolean(state.pending);
  const allowed =
    state.tool === "generate_image"
      ? state.quota?.can_generate
      : state.quota?.can_run;
  $("#run-button").disabled = pending || !state.catalog.configured || !allowed;
  $("#run-button span").textContent = pending
    ? "正在运行…"
    : state.quota && !allowed
      ? "今日次数已用完"
      : "运行工具";
  $("#run-note").textContent = !state.catalog.configured
    ? "试用待配置 · 工具目录可正常浏览"
    : !state.quota
      ? "读取剩余次数后即可试用"
      : state.tool === "generate_image"
        ? "每次生图同时计入试用次数"
        : "搜索和读取全文各计 1 次";
  document.querySelectorAll(".read-button").forEach((node) => {
    node.disabled =
      pending ||
      (node.dataset.example !== "true" &&
        (!state.catalog.configured || !state.quota?.can_run));
  });
  document.querySelectorAll(".result-view").forEach((node) => {
    node.disabled =
      pending ||
      (node.dataset.view === "example"
        ? !state.examples[state.tool]
        : !state.results.has(state.tool));
  });
}
function emptyResult(target, { loading = false, reader = false } = {}) {
  const root = el("div", "empty-result");
  if (loading) root.append(el("span", "spinner"));
  else {
    const box = el("div", "empty-icon");
    box.append(icon(categoryById(toolById(state.tool).category).icon));
    root.append(box);
  }
  root.append(
    el(
      "h4",
      "",
      loading
        ? reader
          ? "正在读取这篇网页"
          : "工具正在运行"
        : "从一个好奇心开始",
    ),
  );
  root.append(
    el(
      "p",
      "",
      loading
        ? "正在等待工具返回真实结果，请稍候。"
        : "示例参数已准备好。\n可以直接运行，也可以修改后再试。",
    ),
  );
  if (!loading) {
    const flow = el("div", "empty-flow");
    flow.append(
      el("span", "", "输入想法"),
      icon("arrow"),
      el("span", "", "运行工具"),
      icon("arrow"),
      el("span", "", "看到结果"),
    );
    root.append(flow);
    if (!state.catalog.configured)
      root.append(
        el(
          "div",
          "unconfigured-note",
          "在线试用尚未配置。维护者配置服务端产品密钥后，即可运行真实工具；内置示例可直接浏览。",
        ),
      );
  }
  target.replaceChildren(root);
}
function errorBox(response) {
  const root = el("div", "error-box");
  const code = response.error?.code || "upstream_error";
  root.append(
    el("h4", "", "这次没有得到可用结果"),
    el("p", "", response.error?.message || "请检查参数或稍后再试。"),
    el("div", "error-code", code),
  );
  if (response.retry_after)
    root.append(el("p", "", `建议 ${response.retry_after} 秒后重试。`));
  const details = response.result?.error;
  if (Array.isArray(details?.candidates)) {
    root.append(
      el("p", "", "有多个同名地点，请根据候选补全城市或地区后再查："),
    );
    for (const candidate of details.candidates)
      root.append(
        el("p", "", candidate.label || candidate.name || pretty(candidate)),
      );
  }
  return root;
}
async function apiRun(tool, arguments_) {
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, arguments: arguments_ }),
      signal: AbortSignal.timeout(tool === "generate_image" ? 175000 : 110000),
    });
    const body = await response.json();
    if (typeof body.ok !== "boolean") throw new Error("unexpected_response");
    if (body.quota || body.error?.code === "quota_unavailable") {
      quotaVersion++;
      state.quota = body.quota || null;
      renderQuota();
    }
    return {
      tool,
      arguments: arguments_,
      ...body,
      http_status: response.status,
      retry_after: response.headers.get("retry-after"),
    };
  } catch (error) {
    quotaVersion++;
    state.quota = null;
    renderQuota();
    return {
      tool,
      arguments: arguments_,
      ok: false,
      error: {
        code: error.name === "TimeoutError" ? "timeout" : "network_error",
        message:
          error.name === "TimeoutError"
            ? "等待超时；上游任务可能仍在执行，请稍后再试。"
            : "未能连接到试用服务，请检查网络后重试。",
      },
    };
  }
}
async function runMain(event) {
  event.preventDefault();
  if (
    state.pending ||
    !state.catalog.configured ||
    !(state.tool === "generate_image"
      ? state.quota?.can_generate
      : state.quota?.can_run)
  )
    return;
  const tool = state.tool;
  const arguments_ = formArguments();
  rememberForm();
  state.previewTools.delete(tool);
  state.pending = { tool };
  if (tool === "search_web") state.reader = null;
  $("#form-error").hidden = true;
  updateRunButton();
  renderResult();
  renderReader();
  const response = await apiRun(tool, arguments_);
  state.results.set(tool, response);
  state.pending = null;
  if (state.tool === tool) {
    renderResult();
    if (!response.ok) {
      $("#form-error").textContent =
        response.error?.message || "调用失败，请查看结果说明。";
      $("#form-error").hidden = false;
    }
    if (matchMedia("(max-width: 640px)").matches)
      $(".output-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  updateRunButton();
}
function showingExample() {
  return state.previewTools.has(state.tool) || !state.results.has(state.tool);
}
function exampleNotice(response) {
  const note = el("div", "example-notice");
  note.append(
    el("strong", "", `示例数据 · ${response.label}`),
    el(
      "p",
      "",
      "固定展示样例，不随左侧参数变化，也不代表实时查询结果。查看示例不调用工具、不扣次数。",
    ),
  );
  return note;
}
function renderResultViews(loading) {
  const root = $("#result-views");
  root.replaceChildren();
  for (const [label, example] of [
    ["示例预览", true],
    ["真实结果", false],
  ]) {
    const node = button(label, "result-view", () => {
      if (example) state.previewTools.add(state.tool);
      else state.previewTools.delete(state.tool);
      state.reader = null;
      renderReader();
      renderResult();
    });
    node.dataset.view = example ? "example" : "live";
    node.setAttribute("aria-pressed", String(showingExample() === example));
    node.disabled = Boolean(
      loading ||
      (!example && !state.results.has(state.tool)) ||
      (example && !state.examples[state.tool]),
    );
    root.append(node);
  }
}
function renderResult() {
  const target = $("#result-main");
  const loading = state.pending?.tool === state.tool && !state.pending.reader;
  renderResultViews(Boolean(state.pending));
  target.setAttribute("aria-busy", String(loading));
  $("#result-meta").textContent = loading ? "RUNNING…" : "READY TO RUN";
  if (loading) return emptyResult(target, { loading: true });
  const response = showingExample()
    ? state.examples[state.tool]
    : state.results.get(state.tool);
  if (!response) return emptyResult(target);
  target.replaceChildren();
  $("#result-meta").textContent = response.example
    ? "示例数据 · 不扣次数"
    : response.duration_ms != null
      ? `${response.ok ? "COMPLETED" : "FAILED"} / ${duration(response.duration_ms)}`
      : "REQUEST FAILED";
  if (response.example) target.append(exampleNotice(response));
  if (!response.ok) target.append(errorBox(response));
  else {
    const result = response.result;
    if (state.tool === "search_web")
      renderSearch(target, result, Boolean(response.example));
    else if (state.tool === "read_page") renderArticle(target, response);
    else if (state.tool === "generate_image") renderImage(target, response);
    else if (state.tool === "get_weather") renderWeather(target, result);
    else if (["search_flights", "search_trains"].includes(state.tool))
      renderTravel(target, result);
    else if (state.tool === "place_search") renderPlaces(target, result);
    else if (state.tool === "route_compare") renderRoutes(target, result);
    else if (state.tool === "get_latest_news") renderTrends(target, result);
    else renderDocument(target, response);
    renderLimitations(target, result);
  }
  target.append(callDetails(response, state.tool));
  updateRunButton();
}
function heading(target, title, badge = "") {
  const root = el("div", "result-heading");
  root.append(el("strong", "", title), el("span", "result-badge", badge));
  target.append(root);
}
function renderSearch(target, result, example = false) {
  const hits = Array.isArray(result.results) ? result.results : [];
  heading(
    target,
    `${hits.length} 条搜索结果`,
    result.meta?.cached ? "CACHED" : result.meta?.provider || "",
  );
  if (!hits.length)
    target.append(el("p", "notice", "没有找到匹配的结果，试试更换关键词。"));
  hits.forEach((hit, index) => {
    const root = el("article", "search-result");
    const source = el("div", "source-line");
    const url = safeUrl(hit.url);
    source.append(
      el("span", "source-index", String(index + 1).padStart(2, "0")),
      el(
        "span",
        "",
        url ? new URL(url).hostname.replace(/^www\./, "") : "来源地址不可用",
      ),
    );
    if (hit.published_at)
      source.append(el("span", "", `· ${hit.published_at}`));
    root.append(
      source,
      link(hit.title || hit.url || "未命名来源", hit.url, "search-title"),
      el("p", "search-snippet", hit.snippet || hit.description || "暂无摘要"),
    );
    const actions = el("div", "search-actions");
    if (url) {
      const read = button(
        example ? "查看示例正文" : "读取全文",
        "read-button",
        () => readFromSearch(url, example),
        "file",
      );
      read.dataset.example = String(example);
      const original = link("原网页", url, "");
      original.append(icon("external"));
      actions.append(read, original);
    }
    root.append(actions);
    target.append(root);
  });
}
async function readFromSearch(url, example = false) {
  if (example) {
    if (state.pending) return;
    state.reader = { url, pending: false, response: state.examples.read_page };
    renderReader();
    return;
  }
  if (state.pending || !state.quota?.can_run) return;
  const requestId = Symbol("read");
  state.reader = { url, pending: true, requestId };
  state.pending = { tool: "read_page", reader: true };
  updateRunButton();
  renderReader();
  const response = await apiRun("read_page", { url });
  if (state.reader?.requestId === requestId)
    state.reader = { url, pending: false, response };
  state.pending = null;
  renderReader();
  updateRunButton();
}
function renderReader() {
  const show = state.tool === "search_web" && Boolean(state.reader);
  $("#reader-panel").hidden = !show;
  $("#workspace-grid").classList.toggle("reading", show);
  $("#result-layout").classList.toggle("has-reader", show);
  if (!show) return;
  const target = $("#reader-content");
  target.setAttribute("aria-busy", String(state.reader.pending));
  if (state.reader.pending)
    emptyResult(target, { loading: true, reader: true });
  else {
    target.replaceChildren();
    const response = state.reader.response;
    if (response.example) target.append(exampleNotice(response));
    if (!response.ok) {
      target.append(errorBox(response));
      const actions = el("div", "article-actions");
      actions.append(
        link("打开原网页", state.reader.url),
        button(
          "重试读取",
          "text-button",
          () => readFromSearch(state.reader.url),
          "arrow",
        ),
      );
      target.append(actions);
    } else renderArticle(target, response, state.reader.url);
    target.append(callDetails(response, "read_page"));
  }
  if (matchMedia("(max-width: 640px)").matches)
    $("#reader-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}
function renderArticle(target, response, originalUrl) {
  const result = response.result;
  target.append(el("h4", "article-title", result.title || "网页正文"));
  const actions = el("div", "article-actions");
  actions.append(
    link("查看原网页", result.url || originalUrl || response.arguments?.url),
    button("复制正文", "text-button", () => copy(result.content || ""), "copy"),
  );
  if (response.duration_ms != null)
    actions.append(el("span", "field-caption", duration(response.duration_ms)));
  target.append(actions);
  if (result.truncated)
    target.append(
      el(
        "p",
        "notice",
        "正文较长，工具返回了截断后的内容。完整信息请查看原网页。",
      ),
    );
  if (result.extracted)
    target.append(
      el(
        "p",
        "notice",
        "已自动提取正文，部分表格或列表可能省略。需要整页时，可在「网页读取」中开启保留整页内容。",
      ),
    );
  if (response.rendered_html) appendMarkdown(target, response.rendered_html);
  else
    target.append(
      el("pre", "raw-content", result.content || "工具未返回正文。"),
    );
}
function appendMarkdown(target, html) {
  const content = el("div", "markdown");
  // HTML 仅来自同源后端经过 Markdown 解析与 nh3 清理后的专用字段。
  content.innerHTML = html;
  content.querySelectorAll("a").forEach((node) => {
    node.target = "_blank";
    node.rel = "noopener noreferrer";
  });
  target.append(content);
}
function renderImage(target, response) {
  const result = response.result;
  heading(
    target,
    response.example ? "图片示例" : "你的画面已生成",
    response.example ? "历史生成样例" : result.cached ? "CACHED" : "GENERATED",
  );
  const file =
    typeof result.image_url === "string" &&
    result.image_url.match(
      /^\/static\/images\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.(?:png|jpeg|jpg|webp))$/,
    );
  const url = file
    ? new URL(`/api/images/${file[1]}`, location.origin).href
    : response.example &&
        /^\/assets\/examples\/[a-z0-9-]+\.(?:png|jpeg|jpg|webp)$/.test(
          result.image_url,
        )
      ? new URL(result.image_url, location.origin).href
      : safeUrl(result.image_url);
  if (url) {
    const image = el("img", "result-image");
    image.alt = response.arguments?.prompt || "工具生成的图片";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () =>
      target.prepend(
        el(
          "p",
          "notice",
          "图片已生成，但预览加载失败。可打开原图或重新加载预览，无需重新生成。",
        ),
      ),
    );
    image.src = url;
    target.append(
      image,
      link("打开图片 / 保存原图", url),
      button("重新加载预览", "text-button", () => {
        target.querySelectorAll(".notice").forEach((node) => node.remove());
        image.src = `${url}${url.includes("?") ? "&" : "?"}retry=${Date.now()}`;
      }),
    );
  } else {
    target.append(
      el(
        "p",
        "notice",
        "图片已生成，但返回的图片地址无法识别，请查看调用详情并联系维护者。",
      ),
    );
  }
  target.append(
    el(
      "p",
      "image-caption",
      [
        result.model,
        result.fallback_used ? "已使用备用模型" : "",
        result.created_at
          ? `生成于 ${observedAt(result.created_at)}（北京时间）`
          : "",
      ]
        .filter(Boolean)
        .join(" · "),
    ),
  );
}
function renderWeather(target, result) {
  target.append(
    el("h4", "weather-city", result.resolved_location || "天气预报"),
  );
  const days = el("div", "weather-days");
  for (const day of result.forecast_days || []) {
    const card = el("div", "weather-day");
    card.append(
      el("p", "", day.date),
      el("strong", "", `${day.high_c}° / ${day.low_c}°`),
      el("span", "", day.day_weather),
      el(
        "span",
        "",
        `${day.day_wind_direction || ""} ${day.day_wind_power || ""}`,
      ),
    );
    days.append(card);
  }
  target.append(days);
  if (!(result.forecast_days || []).length)
    target.append(
      el(
        "p",
        "notice",
        result.forecast_range
          ? `可查询日期为 ${result.forecast_range.first_date} 至 ${result.forecast_range.last_date}，所选日期不在范围内。`
          : "没有覆盖所选日期的预报。",
      ),
    );
  if (result.hourly?.length) {
    const wrap = el("div", "table-wrap");
    const table = el("table", "hourly-table");
    const caption = el(
      "caption",
      "field-caption",
      "逐小时天气（城市当地时间）",
    );
    const head = el("thead");
    const header = el("tr");
    ["时间", "天气", "温度", "降水概率"].forEach((name) => {
      const cell = el("th", "", name);
      cell.scope = "col";
      header.append(cell);
    });
    head.append(header);
    const body = el("tbody");
    for (const hour of result.hourly) {
      const row = el("tr");
      [
        String(hour.time || "")
          .slice(5, 16)
          .replace("T", " "),
        hour.weather,
        `${hour.temp_c}°`,
        hour.pop == null ? "—" : `${hour.pop}%`,
      ].forEach((text) => row.append(el("td", "", text)));
      body.append(row);
    }
    table.append(caption, head, body);
    wrap.append(table);
    target.append(wrap);
  }
  if (result.fetched_at)
    target.append(
      el(
        "p",
        "notice",
        `获取于 ${observedAt(result.fetched_at)}（北京时间）；预报日期与时刻以城市当地时间为准。`,
      ),
    );
}
function renderPlaces(target, result) {
  heading(
    target,
    `找到 ${result.places?.length || 0} 个地点`,
    (result.providers || []).join(" / "),
  );
  if (!result.places?.length)
    target.append(
      el("p", "notice", "没有找到匹配地点，试试补充城市或更换关键词。"),
    );
  for (const place of result.places || []) {
    const card = el("article", "result-card");
    card.append(
      el("h4", "", place.name),
      el("p", "", place.address || place.city),
    );
    const meta = [
      place.rating != null ? `评分 ${place.rating}` : "",
      place.reference_cost_yuan != null
        ? `参考人均 ¥${place.reference_cost_yuan}`
        : "",
      place.distance_m != null ? `相距 ${place.distance_m} 米` : "",
    ].filter(Boolean);
    if (meta.length) card.append(el("p", "", meta.join(" · ")));
    if (place.open_hours)
      card.append(el("p", "", `营业时间：${place.open_hours}`));
    if (place.map_url) card.append(link("在地图中查看", place.map_url));
    target.append(card);
  }
}
function renderRoutes(target, result) {
  const names = {
    driving: "驾车",
    transit: "公共交通",
    walking: "步行",
    bicycling: "骑行",
  };
  heading(
    target,
    `${result.origin?.label || "起点"} → ${result.destination?.label || "终点"}`,
  );
  for (const route of result.routes || []) {
    const card = el("article", "result-card");
    card.append(
      el(
        "h4",
        "",
        `${names[route.mode] || route.mode} · ${minutes(Math.round((route.duration_s || 0) / 60))}`,
      ),
    );
    const meta = [
      route.distance_m != null
        ? `${(route.distance_m / 1000).toFixed(1)} 公里`
        : "",
      route.fare_yuan != null ? `票价 ¥${route.fare_yuan}` : "",
      route.toll_yuan != null ? `过路费 ¥${route.toll_yuan}` : "",
      route.transfers != null ? `换乘 ${route.transfers} 次` : "",
    ].filter(Boolean);
    if (meta.length) card.append(el("p", "", meta.join(" · ")));
    for (const leg of route.legs || [])
      card.append(
        el(
          "p",
          "",
          [leg.line_name, leg.departure_stop, leg.arrival_stop]
            .filter(Boolean)
            .join(" → "),
        ),
      );
    if (route.summary) card.append(el("p", "", route.summary));
    target.append(card);
  }
  if (result.unavailable_modes?.length)
    target.append(
      el(
        "p",
        "notice",
        `部分出行方式未得到结果：${result.unavailable_modes.map((entry) => names[typeof entry === "string" ? entry : entry.mode] || pretty(entry)).join("、")}`,
      ),
    );
  target.append(
    el("p", "notice", "路线为本次查询结果，不代表指定未来时刻的交通情况。"),
  );
}
function renderTravel(target, result) {
  const items = result.services || [];
  heading(
    target,
    `${items.length} 个直达班次`,
    result.observed_at ? observedAt(result.observed_at) : "",
  );
  if (!items.length)
    target.append(el("p", "notice", "当前未找到符合条件的直达班次。"));
  for (const item of items) {
    const card = el("article", "result-card");
    card.append(
      el(
        "h4",
        "",
        [item.number, item.airline || item.train_type, item.travel_class]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    const journey = el("div", "journey");
    const from = el("div");
    const to = el("div");
    from.append(
      el("strong", "", String(item.departure?.time || "").slice(11, 16) || "—"),
      el("span", "", item.departure?.station || item.departure?.city),
    );
    to.append(
      el("strong", "", String(item.arrival?.time || "").slice(11, 16) || "—"),
      el("span", "", item.arrival?.station || item.arrival?.city),
    );
    const line = el("div", "journey-line", minutes(item.duration_minutes));
    if (item.arrival?.day_offset)
      to.append(el("span", "", `到达日期 +${item.arrival.day_offset} 天`));
    journey.append(from, line, to);
    card.append(journey);
    const bottom = el("div", "result-footer");
    bottom.append(
      el("span", "", "查询时参考价"),
      el(
        "strong",
        "price",
        item.price_yuan != null ? `¥${item.price_yuan}` : "未提供",
      ),
    );
    card.append(bottom);
    if (item.booking_url) card.append(link("查看供应商页面", item.booking_url));
    target.append(card);
  }
  target.append(
    el(
      "p",
      "notice",
      "出发与到达时刻均为当地时间；价格为查询时参考价，预订前请向供应商核对。",
    ),
  );
}
function renderTrends(target, result) {
  const items = Array.isArray(result.data)
    ? result.data
    : Array.isArray(result.items)
      ? result.items
      : [];
  heading(target, `${items.length} 条热榜资讯`, "LATEST SNAPSHOT");
  if (!items.length)
    target.append(el("p", "notice", "最近一批暂无热榜资讯，请稍后再试。"));
  for (const item of items) {
    const card = el("article", "search-result");
    card.append(
      el(
        "div",
        "source-line",
        `${item.platform_name || item.platform || "热榜"} · ${item.rank != null ? `第 ${item.rank} 名` : ""}`,
      ),
      link(item.title || "未命名资讯", item.url, "search-title"),
    );
    if (item.timestamp) {
      const time = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(item.timestamp)
        ? `${item.timestamp.replace(" ", "T")}+08:00`
        : item.timestamp;
      const fetched = observedAt(time);
      if (fetched)
        card.append(el("p", "field-caption", `抓取于 ${fetched}（北京时间）`));
    }
    target.append(card);
  }
  target.append(
    el("p", "notice", "展示各平台最近一次成功抓取的数据，不代表实时榜单。"),
  );
}
function renderDocument(target, response) {
  if (response.rendered_html) appendMarkdown(target, response.rendered_html);
  else
    target.append(
      el(
        "pre",
        "raw-content",
        response.result.content || pretty(response.result),
      ),
    );
  if (state.tool === "resolve-library-id")
    target.append(
      el(
        "p",
        "notice",
        "找到合适的库 ID 后，切换到「查询文档」，填写 ID 和问题继续查询。",
      ),
    );
}
function renderLimitations(target, result) {
  const notes = {
    filtered_partial_list:
      "结果来自供应商返回的部分班次，可能还有其他符合条件的选项。",
    other_class_returned: "部分班次返回的舱位或座席与请求不同，请以结果为准。",
    high_speed_not_covered: "供应商的这批结果未覆盖高铁班次。",
    requested_date_not_covered: "所选日期不在预报覆盖范围内。",
    fewer_days: "供应商返回的预报天数少于请求范围。",
    hourly_unavailable: "逐小时预报暂不可用。",
    first_term_only: "当前来源只搜索了第一个关键词。",
  };
  for (const entry of result.limitations || []) {
    const code = typeof entry === "string" ? entry : entry.code;
    target.append(
      el(
        "p",
        "notice",
        notes[code] ||
          `结果限制：${typeof entry === "string" ? entry : pretty(entry)}`,
      ),
    );
  }
  if (result.time_filter_relaxed)
    target.append(
      el("p", "notice", "指定时间范围内结果不足，本次搜索放宽了时效条件。"),
    );
}
function callDetails(response, id) {
  const details = el("details", "call-details");
  const summary = el(
    "summary",
    "",
    response.example ? "示例数据与接入参数" : "调用详情与接入参数",
  );
  details.append(summary);
  const tool = toolById(id);
  const path = tool.path || `/${tool.service}/mcp`;
  const args =
    response.arguments ||
    state.values.get(id) ||
    (id === "read_page" && state.reader ? { url: state.reader.url } : {});
  const payload = tool.path
    ? args
    : {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: id, arguments: args },
      };
  details.append(el("p", "call-caption", `POST <网关地址>${path}`));
  if (!tool.path)
    details.append(
      el(
        "p",
        "field-caption",
        "MCP 调用需先完成 initialize 握手；产品密钥通过 Authorization 请求头提供。",
      ),
    );
  details.append(
    button("复制参数", "text-button", () => copy(pretty(payload)), "copy"),
    el("pre", "", pretty(payload)),
    el(
      "p",
      "call-caption",
      response.example ? "示例响应 · 非本次调用" : "RESPONSE",
    ),
    el("pre", "", pretty(response.result || response.error)),
  );
  return details;
}
function renderCatalog(query = "") {
  const matches = state.catalog.categories.filter((category) =>
    `${category.title} ${category.en} ${category.providers.join(" ")} ${category.description}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  $("#catalog-count").textContent = `${matches.length} 类工具能力`;
  $("#integration-grid").replaceChildren(
    ...matches.map((category) => {
      const index = state.catalog.categories.indexOf(category);
      const card = button("", "integration-card", () => {
        selectCategory(category.id);
        $("#workspace").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      card.setAttribute("aria-label", `试用${category.title}`);
      const top = el("div", "card-top");
      const box = el("span", "tool-icon");
      box.append(icon(category.icon));
      top.append(
        box,
        el("span", "card-number", String(index + 1).padStart(2, "0")),
      );
      const bottom = el("div", "card-bottom");
      bottom.append(
        el("span", "providers", category.providers.join(" / ")),
        icon("arrow"),
      );
      const protocols = el("div", "protocol-tags");
      category.tags.forEach((tag) =>
        protocols.append(el("span", "protocol-tag", tag)),
      );
      card.append(
        top,
        el("h3", "", category.title),
        protocols,
        el("p", "", category.description),
        bottom,
      );
      return card;
    }),
  );
  if (!matches.length)
    $("#integration-grid").append(
      el(
        "p",
        "empty-catalog",
        "没有匹配的工具，试试搜索「地图」「天气」或服务商名称。",
      ),
    );
}
async function init() {
  try {
    const response = await fetch("/api/catalog");
    if (!response.ok) throw new Error("catalog_unavailable");
    state.catalog = await response.json();
    try {
      const examples = await fetch("/api/examples");
      if (examples.ok) state.examples = (await examples.json()).samples || {};
    } catch {
      // 示例资料不可用时仍保留真实试用入口。
    }
    buildNavigation();
    renderCatalog();
    const configured = state.catalog.configured;
    const status = $("#connection-status");
    status.classList.toggle("is-ready", configured);
    status.replaceChildren(
      el("span", "status-dot"),
      document.createTextNode(configured ? "试用入口已配置" : "试用待配置"),
    );
    const requested = new URLSearchParams(location.hash.replace(/^#/, "")).get(
      "tool",
    );
    selectTool(toolById(requested) ? requested : "search_web");
    await refreshQuota();
    $("#tool-form").addEventListener("submit", runMain);
    $("#form-fields").addEventListener("input", updateExampleSelection);
    $("#form-fields").addEventListener("change", updateExampleSelection);
    $("#tool-form").addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        if (!$("#run-button").disabled) $("#tool-form").requestSubmit();
      }
    });
    $("#run-button kbd").textContent = /Mac|iPhone|iPad/.test(
      navigator.platform,
    )
      ? "⌘↵"
      : "Ctrl↵";
    $("#catalog-search").addEventListener("input", (event) =>
      renderCatalog(event.target.value),
    );
    $("#close-reader").addEventListener("click", () => {
      state.reader = null;
      renderReader();
    });
    window.addEventListener("focus", () => {
      if (!state.pending) refreshQuota();
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !state.pending) refreshQuota();
    });
  } catch {
    $("#connection-status").textContent = "目录暂不可用";
    $("#run-button").disabled = true;
    $("#result-main").append(
      errorBox({
        error: {
          code: "catalog_unavailable",
          message: "无法获取工具目录，请刷新页面或联系维护者。",
        },
      }),
    );
  }
}
init();
