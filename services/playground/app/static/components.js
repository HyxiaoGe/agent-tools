// 表单的展示与交互留在页面层，提交值仍使用标准 FormData。
const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
};
const glyph = (path) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(svg.namespaceURI, "path");
  line.setAttribute("d", path);
  svg.append(line);
  return svg;
};
const changed = (input) => {
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
};
let activeLayer = null;

export function closeControls() {
  activeLayer?.close(false);
}

function placePanel(panel, anchor, preferredWidth) {
  const box = anchor.getBoundingClientRect();
  const width = Math.min(preferredWidth || box.width, window.innerWidth - 24);
  panel.style.width = `${width}px`;
  const height = panel.getBoundingClientRect().height;
  const below = window.innerHeight - box.bottom - 12;
  const top =
    below >= height || box.top < height + 12
      ? box.bottom + 8
      : box.top - height - 8;
  panel.style.left = `${Math.max(12, Math.min(box.left, window.innerWidth - width - 12))}px`;
  panel.style.top = `${Math.max(12, Math.min(top, window.innerHeight - height - 12))}px`;
}

document.addEventListener("pointerdown", (event) => {
  if (
    activeLayer &&
    !activeLayer.panel.contains(event.target) &&
    !activeLayer.anchor.contains(event.target)
  ) {
    activeLayer.close(false);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && activeLayer) {
    event.preventDefault();
    activeLayer.close(true);
  }
});
document.addEventListener("focusin", (event) => {
  if (
    activeLayer &&
    !activeLayer.panel.contains(event.target) &&
    !activeLayer.anchor.contains(event.target)
  ) {
    activeLayer.close(false);
  }
});
window.addEventListener("resize", () => activeLayer?.reposition());
document.addEventListener(
  "scroll",
  (event) => {
    if (activeLayer && !activeLayer.panel.contains(event.target))
      activeLayer.reposition();
  },
  true,
);

function storedValue(field, value) {
  const input = make("input");
  input.type = "hidden";
  input.name = field.name;
  input.value = value;
  return input;
}
const optionsOf = (field) =>
  field.options.map((option) =>
    typeof option === "object"
      ? { value: String(option.value), label: option.label }
      : { value: String(option), label: String(option) },
  );

export function createSelect(field, value, labelId) {
  const root = make("div", "select-control");
  const options = optionsOf(field);
  const input = storedValue(field, value);
  if (!options.some((entry) => entry.value === input.value))
    input.value = options[0].value;
  const trigger = make("button", "select-trigger");
  trigger.type = "button";
  trigger.id = `field-${field.name}`;
  trigger.setAttribute("role", "combobox");
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-labelledby", labelId);
  const display = make("span", "select-value");
  trigger.append(display, glyph("m7 10 5 5 5-5"));
  const updateLabel = () => {
    display.textContent = options.find(
      (entry) => entry.value === input.value,
    ).label;
  };
  updateLabel();
  let popup = null;
  let activeIndex = 0;
  let search = "";
  let searchTimer;
  const close = (focus) => {
    if (!popup) return;
    const current = popup;
    popup = null;
    if (activeLayer?.panel === current) activeLayer = null;
    current.remove();
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
    trigger.removeAttribute("aria-controls");
    clearTimeout(searchTimer);
    search = "";
    if (focus) trigger.focus({ preventScroll: true });
  };
  const highlight = (index) => {
    activeIndex = (index + options.length) % options.length;
    [...popup.children].forEach((node, position) =>
      node.classList.toggle("is-highlighted", position === activeIndex),
    );
    trigger.setAttribute(
      "aria-activedescendant",
      popup.children[activeIndex].id,
    );
    popup.children[activeIndex].scrollIntoView({ block: "nearest" });
  };
  const choose = (index) => {
    input.value = options[index].value;
    updateLabel();
    changed(input);
    close(true);
  };
  const open = () => {
    if (popup) return;
    closeControls();
    popup = make("div", "control-popover select-menu");
    popup.id = `list-${field.name}`;
    popup.setAttribute("role", "listbox");
    popup.setAttribute("aria-labelledby", labelId);
    popup.setAttribute("popover", "manual");
    options.forEach((option, index) => {
      const node = make("div", "select-option");
      node.id = `option-${field.name}-${index}`;
      node.setAttribute("role", "option");
      node.setAttribute("aria-selected", String(option.value === input.value));
      node.append(make("span", "", option.label), glyph("m5 12 4 4L20 5"));
      node.addEventListener("pointerdown", (event) => event.preventDefault());
      node.addEventListener("click", () => choose(index));
      node.addEventListener("pointermove", () => highlight(index));
      popup.append(node);
    });
    document.body.append(popup);
    popup.showPopover();
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-controls", popup.id);
    placePanel(
      popup,
      trigger,
      Math.max(210, trigger.getBoundingClientRect().width),
    );
    activeLayer = {
      panel: popup,
      anchor: root,
      close,
      reposition: () =>
        placePanel(
          popup,
          trigger,
          Math.max(210, trigger.getBoundingClientRect().width),
        ),
    };
    highlight(
      Math.max(
        0,
        options.findIndex((entry) => entry.value === input.value),
      ),
    );
  };
  trigger.addEventListener("click", () => (popup ? close(true) : open()));
  trigger.addEventListener("keydown", (event) => {
    if (
      ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)
    ) {
      event.preventDefault();
      const wasOpen = Boolean(popup);
      open();
      if (["Enter", " "].includes(event.key)) {
        if (wasOpen) choose(activeIndex);
      } else if (event.key === "Home") highlight(0);
      else if (event.key === "End") highlight(options.length - 1);
      else if (wasOpen)
        highlight(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Tab" && popup) {
      input.value = options[activeIndex].value;
      updateLabel();
      changed(input);
      close(false);
    } else if (
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      search += event.key.toLowerCase();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        search = "";
      }, 600);
      open();
      const index = options.findIndex((entry) =>
        entry.label.toLowerCase().startsWith(search),
      );
      if (index >= 0) highlight(index);
    }
  });
  root.append(input, trigger);
  return root;
}

function ratioPreview(value) {
  const [width, height] = value.split(":").map(Number);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 40 32");
  svg.setAttribute("class", "ratio-preview");
  svg.setAttribute("aria-hidden", "true");
  const shape = document.createElementNS(svg.namespaceURI, "rect");
  const scale = Math.min(27 / width, 23 / height);
  shape.setAttribute("x", (40 - width * scale) / 2);
  shape.setAttribute("y", (32 - height * scale) / 2);
  shape.setAttribute("width", width * scale);
  shape.setAttribute("height", height * scale);
  shape.setAttribute("rx", "3");
  svg.append(shape);
  return svg;
}

export function createChoices(field, value, labelId) {
  const root = make(
    "div",
    `choice-control ${field.name === "aspect_ratio" ? "ratio-control" : field.name === "quality" ? "quality-control" : "segmented-control"}`,
  );
  root.setAttribute("role", "radiogroup");
  root.setAttribute("aria-labelledby", labelId);
  const input = storedValue(field, value);
  const options = optionsOf(field);
  if (!options.some((entry) => entry.value === input.value))
    input.value = options[0].value;
  const buttons = [];
  const select = (index, focus = false) => {
    input.value = options[index].value;
    buttons.forEach((node, position) => {
      node.setAttribute("aria-checked", String(position === index));
      node.tabIndex = position === index ? 0 : -1;
    });
    if (focus) buttons[index].focus();
    changed(input);
  };
  options.forEach((option, index) => {
    const node = make("button", "choice-option");
    node.type = "button";
    node.setAttribute("role", "radio");
    node.setAttribute("aria-label", option.label);
    if (field.name === "aspect_ratio") node.append(ratioPreview(option.value));
    if (field.name === "quality")
      node.append(
        glyph(
          option.value === "standard"
            ? "m13 2-9 12h7l-1 8 10-13h-8l1-7Z"
            : "m12 3 3 6 6 3-6 3-3 6-3-6-6-3 6-3 3-6Z",
        ),
      );
    node.append(make("span", "choice-label", option.label));
    if (field.name === "quality")
      node.append(
        make(
          "span",
          "choice-description",
          option.value === "standard" ? "快速探索灵感" : "更精细的画面",
        ),
      );
    node.addEventListener("click", () => select(index));
    node.addEventListener("keydown", (event) => {
      let next = index;
      if (["ArrowRight", "ArrowDown"].includes(event.key))
        next = (index + 1) % options.length;
      else if (["ArrowLeft", "ArrowUp"].includes(event.key))
        next = (index + options.length - 1) % options.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = options.length - 1;
      else return;
      event.preventDefault();
      select(next, true);
    });
    buttons.push(node);
    root.append(node);
  });
  root.append(input);
  select(options.findIndex((entry) => entry.value === input.value));
  return root;
}

const iso = (date) =>
  `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
function dateOf(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const result = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(result.valueOf()) || iso(result) !== value
    ? null
    : result;
}
const todayInShanghai = () =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
const shiftMonth = (date, delta) => {
  const next = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + delta, 1),
  );
  const last = new Date(
    Date.UTC(next.getUTCFullYear(), next.getUTCMonth() + 1, 0),
  ).getUTCDate();
  next.setUTCDate(Math.min(date.getUTCDate(), last));
  return next;
};

export function createDatePicker(field, value, labelId) {
  const root = make("div", "date-control");
  const input = make("input", "date-input");
  input.id = `field-${field.name}`;
  input.name = field.name;
  input.type = "text";
  input.value = value;
  input.placeholder = field.placeholder || "选择日期";
  input.required = Boolean(field.required);
  input.pattern = "\\d{4}-\\d{2}-\\d{2}";
  input.maxLength = 10;
  input.setAttribute("aria-labelledby", labelId);
  input.autocomplete = "off";
  const hint = make(
    "span",
    "sr-only",
    "日期格式为 YYYY-MM-DD，可输入或打开日历选择。",
  );
  hint.id = `hint-${field.name}`;
  input.setAttribute("aria-describedby", hint.id);
  const trigger = make("button", "date-trigger");
  trigger.type = "button";
  trigger.setAttribute(
    "aria-label",
    `选择${field.label.replace("（可选）", "")}`,
  );
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  trigger.append(
    glyph(
      "M7 2v4m10-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Zm2 10h2m3 0h2m3 0h1M7 17h2m3 0h2",
    ),
  );
  const min = field.name === "departure_date" ? todayInShanghai() : "";
  const validate = () => {
    const bad =
      input.value && (!dateOf(input.value) || (min && input.value < min));
    input.setCustomValidity(
      bad
        ? min && input.value < min
          ? "出发日期不能早于北京时间今天"
          : "请输入有效日期，格式为 YYYY-MM-DD"
        : "",
    );
    input.setAttribute("aria-invalid", String(Boolean(bad)));
  };
  input.addEventListener("input", validate);
  let panel = null;
  let focused = null;
  const close = (focus) => {
    if (!panel) return;
    const current = panel;
    panel = null;
    if (activeLayer?.panel === current) activeLayer = null;
    current.close();
    current.remove();
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-controls");
    if (focus) trigger.focus({ preventScroll: true });
  };
  const choose = (value) => {
    input.value = value;
    changed(input);
    close(true);
  };
  const draw = (focusDay = false) => {
    panel.replaceChildren();
    const heading = make("div", "calendar-heading");
    const title = make(
      "h4",
      "",
      `${focused.getUTCFullYear()} 年 ${focused.getUTCMonth() + 1} 月`,
    );
    title.setAttribute("aria-live", "polite");
    const monthButton = (delta, label, path) => {
      const node = make("button", "calendar-nav");
      node.type = "button";
      node.setAttribute("aria-label", label);
      node.append(glyph(path));
      node.addEventListener("click", () => {
        focused = shiftMonth(focused, delta);
        draw();
        placePanel(panel, root, 316);
        panel
          .querySelector(
            delta < 0 ? '[aria-label="上个月"]' : '[aria-label="下个月"]',
          )
          .focus();
      });
      return node;
    };
    heading.append(
      title,
      monthButton(-1, "上个月", "m14 6-6 6 6 6"),
      monthButton(1, "下个月", "m10 6 6 6-6 6"),
    );
    const grid = make("table", "calendar-grid");
    grid.setAttribute("role", "grid");
    grid.setAttribute("aria-label", title.textContent);
    const head = make("thead");
    const week = make("tr");
    ["一", "二", "三", "四", "五", "六", "日"].forEach((name) => {
      const day = make("th", "", name);
      day.scope = "col";
      day.setAttribute("abbr", `星期${name}`);
      week.append(day);
    });
    head.append(week);
    const body = make("tbody");
    const first = new Date(
      Date.UTC(focused.getUTCFullYear(), focused.getUTCMonth(), 1),
    );
    const start = new Date(first);
    start.setUTCDate(1 - ((first.getUTCDay() + 6) % 7));
    const today = todayInShanghai();
    for (let row = 0; row < 6; row++) {
      const line = make("tr");
      for (let column = 0; column < 7; column++) {
        const date = new Date(start);
        date.setUTCDate(start.getUTCDate() + row * 7 + column);
        const value = iso(date);
        const cell = make("td");
        cell.setAttribute("role", "gridcell");
        cell.setAttribute("aria-selected", String(value === input.value));
        const day = make("button", "calendar-day", date.getUTCDate());
        day.type = "button";
        day.dataset.date = value;
        day.tabIndex = value === iso(focused) ? 0 : -1;
        day.disabled = Boolean(min && value < min);
        day.classList.toggle(
          "outside-month",
          date.getUTCMonth() !== focused.getUTCMonth(),
        );
        day.setAttribute(
          "aria-label",
          `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日`,
        );
        if (value === today) day.setAttribute("aria-current", "date");
        day.addEventListener("click", () => choose(value));
        day.addEventListener("keydown", (event) => {
          const deltas = {
            ArrowRight: 1,
            ArrowLeft: -1,
            ArrowDown: 7,
            ArrowUp: -7,
            Home: -column,
            End: 6 - column,
          };
          if (event.key === "PageUp" || event.key === "PageDown") {
            focused = shiftMonth(
              date,
              (event.key === "PageUp" ? -1 : 1) * (event.shiftKey ? 12 : 1),
            );
          } else if (event.key in deltas) {
            focused = new Date(date);
            focused.setUTCDate(date.getUTCDate() + deltas[event.key]);
          } else return;
          event.preventDefault();
          if (min && iso(focused) < min) focused = dateOf(min);
          draw(true);
        });
        cell.append(day);
        line.append(cell);
      }
      body.append(line);
    }
    grid.append(head, body);
    const footer = make("div", "calendar-footer");
    const todayButton = make("button", "calendar-today", "今天");
    todayButton.type = "button";
    todayButton.addEventListener("click", () => choose(today));
    footer.append(make("span", "", "方向键选日期 · Esc 关闭"), todayButton);
    if (!field.required) {
      const clear = make("button", "calendar-clear", "清除");
      clear.type = "button";
      clear.addEventListener("click", () => choose(""));
      footer.append(clear);
    }
    panel.append(heading, grid, footer);
    if (focusDay)
      panel
        .querySelector(`[data-date="${iso(focused)}"]`)
        ?.focus({ preventScroll: true });
  };
  const open = () => {
    if (panel) return close(true);
    closeControls();
    focused = dateOf(input.value) || dateOf(todayInShanghai());
    if (min && iso(focused) < min) focused = dateOf(min);
    panel = make("dialog", "control-popover calendar-panel");
    panel.id = `calendar-${field.name}`;
    panel.setAttribute(
      "aria-label",
      `${field.label.replace("（可选）", "")}日历`,
    );
    panel.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(true);
    });
    panel.addEventListener("pointerdown", (event) => {
      const rect = panel.getBoundingClientRect();
      if (
        event.target === panel &&
        (event.clientX < rect.left ||
          event.clientX > rect.right ||
          event.clientY < rect.top ||
          event.clientY > rect.bottom)
      )
        close(true);
    });
    document.body.append(panel);
    activeLayer = {
      panel,
      anchor: root,
      close,
      reposition: () => placePanel(panel, root, 316),
    };
    draw();
    panel.showModal();
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-controls", panel.id);
    placePanel(panel, root, 316);
    panel
      .querySelector(`[data-date="${iso(focused)}"]`)
      ?.focus({ preventScroll: true });
  };
  trigger.addEventListener("click", open);
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      open();
    }
  });
  root.append(input, trigger, hint);
  validate();
  return root;
}
