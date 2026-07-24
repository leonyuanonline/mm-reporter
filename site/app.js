const state = {
  manifest: null,
  rows: [],
  filteredRows: [],
  sortKey: "证券代码",
  sortDirection: "asc",
  reportBase: "./reports",
  reportAvailable: false,
  calendarView: "",
};

const elements = {
  date: document.querySelector("#report-date"),
  dateDisplay: document.querySelector("#selected-date"),
  calendarToggle: document.querySelector("#calendar-toggle"),
  calendarPopover: document.querySelector("#calendar-popover"),
  calendarMonth: document.querySelector("#calendar-month"),
  calendarGrid: document.querySelector("#calendar-grid"),
  previousMonth: document.querySelector("#previous-month"),
  nextMonth: document.querySelector("#next-month"),
  previous: document.querySelector("#previous-date"),
  next: document.querySelector("#next-date"),
  exchange: document.querySelector("#exchange-filter"),
  action: document.querySelector("#action-filter"),
  service: document.querySelector("#service-filter"),
  maker: document.querySelector("#maker-filter"),
  code: document.querySelector("#code-filter"),
  clear: document.querySelector("#clear-filters"),
  body: document.querySelector("#report-body"),
  tableWrap: document.querySelector("#table-wrap"),
  message: document.querySelector("#state-message"),
  visibleCount: document.querySelector("#visible-count"),
  summaryLabel: document.querySelector("#summary-label"),
  latestDate: document.querySelector("#latest-date"),
  download: document.querySelector("#download-link"),
};

async function fetchManifest() {
  const candidates = ["./reports/index.json", "../reports/index.json"];
  for (const url of candidates) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) continue;
      state.reportBase = url.replace(/\/index\.json$/, "");
      return await response.json();
    } catch (_) {
      // Try the alternate path for local /site/ previews.
    }
  }
  throw new Error("未找到报告索引。请先运行索引生成脚本。");
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const source = text.replace(/^\uFEFF/, "");

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  const headers = rows.shift() || [];
  return rows
    .filter((values) => values.some((value) => value !== ""))
    .map((values) => Object.fromEntries(headers.map((header, i) => [header, values[i] || ""])));
}

function optionValues(key) {
  return [...new Set(state.rows.map((row) => row[key]).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function populateFilter(select, values, label) {
  const selected = select.value;
  select.replaceChildren(new Option(label, ""));
  values.forEach((value) => select.add(new Option(value, value)));
  select.value = values.includes(selected) ? selected : "";
}

function updateFilterOptions() {
  populateFilter(elements.exchange, optionValues("交易所"), "全部交易所");
  populateFilter(elements.action, optionValues("动作"), "全部动作");
  populateFilter(elements.service, optionValues("服务类型原文"), "全部服务类型");
}

function textMatches(value, query) {
  return String(value || "").toLocaleLowerCase("zh-CN")
    .includes(query.trim().toLocaleLowerCase("zh-CN"));
}

function applyFilters() {
  state.filteredRows = state.rows.filter((row) =>
    (!elements.exchange.value || row["交易所"] === elements.exchange.value) &&
    (!elements.action.value || row["动作"] === elements.action.value) &&
    (!elements.service.value || row["服务类型原文"] === elements.service.value) &&
    textMatches(row["做市商"], elements.maker.value) &&
    textMatches(row["证券代码"], elements.code.value)
  );
  state.filteredRows.sort((left, right) => {
    const result = String(left[state.sortKey] || "").localeCompare(
      String(right[state.sortKey] || ""),
      "zh-CN",
      { numeric: true },
    );
    return state.sortDirection === "asc" ? result : -result;
  });
  renderTable();
  updateUrl();
}

function makeCell(value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value || "—";
  return cell;
}

function renderTable() {
  elements.body.replaceChildren();
  state.filteredRows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.append(
      makeCell(row["公告发布日期"]),
      makeCell(row["交易所"], "exchange"),
      makeCell(row["做市商"]),
      makeCell(row["证券代码"], "code"),
      makeCell(row["生效日期"]),
      makeCell(row["动作"], "action"),
      makeCell(row["服务类型原文"]),
    );
    const sourceCell = document.createElement("td");
    const firstUrl = String(row["来源URL"] || "").split(";")[0].trim();
    if (/^https?:\/\//i.test(firstUrl)) {
      const link = document.createElement("a");
      link.className = "source-link";
      link.href = firstUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "查看公告";
      sourceCell.append(link);
    } else {
      sourceCell.textContent = "—";
    }
    tr.append(sourceCell);
    elements.body.append(tr);
  });

  const total = state.rows.length;
  elements.visibleCount.textContent = state.filteredRows.length.toLocaleString("zh-CN");
  elements.summaryLabel.textContent = state.filteredRows.length === total
    ? `条记录`
    : `条结果，共 ${total.toLocaleString("zh-CN")} 条`;
  elements.tableWrap.hidden = state.filteredRows.length === 0;
  elements.message.hidden = state.filteredRows.length > 0;
  elements.message.textContent = !state.reportAvailable
    ? "该日期暂无可用报告，请选择其他日期。"
    : total === 0
    ? "该日期没有公告记录。"
    : "没有符合当前筛选条件的记录。";

  document.querySelectorAll("th[data-key]").forEach((header) => {
    header.dataset.sort = header.dataset.key === state.sortKey ? state.sortDirection : "";
  });
}

function adjacentReport(offset) {
  const reports = state.manifest.reports;
  const selectedDate = elements.date.value;
  const index = reports.findIndex((report) => report.date === selectedDate);
  if (index >= 0) return reports[index + offset];
  if (offset > 0) return reports.find((report) => report.date < selectedDate);
  return [...reports].reverse().find((report) => report.date > selectedDate);
}

function updateDateButtons() {
  elements.previous.disabled = !adjacentReport(1);
  elements.next.disabled = !adjacentReport(-1);
}

function monthKey(year, monthIndex) {
  return `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
}

function shiftMonthKey(value, offset) {
  const [year, month] = value.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1 + offset, 1));
  return monthKey(shifted.getUTCFullYear(), shifted.getUTCMonth());
}

function closeCalendar() {
  elements.calendarPopover.hidden = true;
  elements.calendarToggle.setAttribute("aria-expanded", "false");
}

function renderCalendar() {
  if (!state.calendarView || !state.manifest) return;
  const [year, month] = state.calendarView.split("-").map(Number);
  const reports = new Map(state.manifest.reports.map((report) => [report.date, report]));
  const firstWeekday = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7;
  const dayCount = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const earliestMonth = state.manifest.reports[state.manifest.reports.length - 1].date.slice(0, 7);
  const latestMonth = state.manifest.reports[0].date.slice(0, 7);

  elements.calendarMonth.textContent = `${year} 年 ${month} 月`;
  elements.previousMonth.disabled = state.calendarView <= earliestMonth;
  elements.nextMonth.disabled = state.calendarView >= latestMonth;
  elements.calendarGrid.replaceChildren();

  for (let index = 0; index < firstWeekday; index += 1) {
    const spacer = document.createElement("span");
    spacer.className = "calendar-spacer";
    elements.calendarGrid.append(spacer);
  }

  for (let day = 1; day <= dayCount; day += 1) {
    const date = `${state.calendarView}-${String(day).padStart(2, "0")}`;
    const report = reports.get(date);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "calendar-day";
    button.setAttribute("role", "gridcell");
    button.dataset.date = date;
    if (date === elements.date.value) {
      button.classList.add("selected");
      button.setAttribute("aria-current", "date");
    }

    const dayNumber = document.createElement("span");
    dayNumber.className = "calendar-day-number";
    dayNumber.textContent = day;
    const count = document.createElement("span");
    count.className = "calendar-day-count";

    if (!report) {
      button.classList.add("missing");
      button.disabled = true;
      button.setAttribute("aria-label", `${date}，报告缺失`);
      count.textContent = "—";
    } else if (report.records === 0) {
      button.classList.add("empty");
      button.setAttribute("aria-label", `${date}，无公告`);
      count.textContent = "0";
    } else {
      button.classList.add("has-records");
      button.setAttribute("aria-label", `${date}，${report.records} 条公告`);
      count.textContent = report.records > 99 ? "99+" : report.records;
    }
    button.append(dayNumber, count);
    elements.calendarGrid.append(button);
  }
}

async function loadReport(date) {
  const report = state.manifest.reports.find((item) => item.date === date);
  if (!report) {
    state.rows = [];
    state.reportAvailable = false;
    elements.download.hidden = true;
    elements.download.removeAttribute("href");
    updateFilterOptions();
    updateDateButtons();
    applyFilters();
    return;
  }
  state.reportAvailable = true;
  elements.message.hidden = false;
  elements.tableWrap.hidden = true;
  elements.message.textContent = "正在读取报告…";
  const url = `${state.reportBase}/${encodeURIComponent(report.file)}`;
  elements.download.href = url;
  elements.download.hidden = false;
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.rows = parseCsv(await response.text());
    updateFilterOptions();
    updateDateButtons();
    applyFilters();
  } catch (error) {
    state.rows = [];
    elements.visibleCount.textContent = "!";
    elements.summaryLabel.textContent = "报告读取失败";
    elements.message.hidden = false;
    elements.message.textContent = `无法读取 ${date} 的报告，请稍后重试。`;
  }
}

function updateUrl() {
  const params = new URLSearchParams();
  if (elements.date.value) params.set("date", elements.date.value);
  if (elements.exchange.value) params.set("exchange", elements.exchange.value);
  if (elements.action.value) params.set("action", elements.action.value);
  if (elements.service.value) params.set("service", elements.service.value);
  if (elements.maker.value) params.set("maker", elements.maker.value);
  if (elements.code.value) params.set("code", elements.code.value);
  history.replaceState(null, "", `${location.pathname}?${params}${location.hash}`);
}

function restoreFilters(params) {
  elements.exchange.value = params.get("exchange") || "";
  elements.action.value = params.get("action") || "";
  elements.service.value = params.get("service") || "";
  elements.maker.value = params.get("maker") || "";
  elements.code.value = params.get("code") || "";
}

function shiftDate(offset) {
  const target = adjacentReport(offset);
  if (target) {
    selectReportDate(target.date);
  }
}

function selectReportDate(date, close = true) {
  elements.date.value = date;
  elements.dateDisplay.textContent = date;
  state.calendarView = date.slice(0, 7);
  renderCalendar();
  if (close) closeCalendar();
  loadReport(date);
}

async function initialize() {
  try {
    state.manifest = await fetchManifest();
    if (!Array.isArray(state.manifest.reports) || state.manifest.reports.length === 0) {
      throw new Error("目前还没有可显示的历史报告。");
    }
    elements.latestDate.textContent = `最新报告：${state.manifest.latest}`;
    const params = new URLSearchParams(location.search);
    const requestedDate = params.get("date");
    const initialDate = state.manifest.reports.some((report) => report.date === requestedDate)
      ? requestedDate
      : state.manifest.latest;
    elements.date.value = initialDate;
    elements.dateDisplay.textContent = initialDate;
    state.calendarView = initialDate.slice(0, 7);
    renderCalendar();
    await loadReport(initialDate);
    restoreFilters(params);
    applyFilters();
  } catch (error) {
    elements.visibleCount.textContent = "!";
    elements.summaryLabel.textContent = "载入失败";
    elements.message.textContent = error.message;
  }
}

elements.calendarToggle.addEventListener("click", () => {
  const willOpen = elements.calendarPopover.hidden;
  if (willOpen) {
    state.calendarView = elements.date.value.slice(0, 7);
    renderCalendar();
    elements.calendarPopover.hidden = false;
    elements.calendarToggle.setAttribute("aria-expanded", "true");
  } else {
    closeCalendar();
  }
});
elements.calendarGrid.addEventListener("click", (event) => {
  const day = event.target.closest(".calendar-day:not(:disabled)");
  if (day) selectReportDate(day.dataset.date);
});
elements.previousMonth.addEventListener("click", () => {
  state.calendarView = shiftMonthKey(state.calendarView, -1);
  renderCalendar();
});
elements.nextMonth.addEventListener("click", () => {
  state.calendarView = shiftMonthKey(state.calendarView, 1);
  renderCalendar();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".date-control")) closeCalendar();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.calendarPopover.hidden) {
    closeCalendar();
    elements.calendarToggle.focus();
  }
});
elements.previous.addEventListener("click", () => shiftDate(1));
elements.next.addEventListener("click", () => shiftDate(-1));
[elements.exchange, elements.action, elements.service].forEach((element) =>
  element.addEventListener("change", applyFilters));
[elements.maker, elements.code].forEach((element) =>
  element.addEventListener("input", applyFilters));
elements.clear.addEventListener("click", () => {
  [elements.exchange, elements.action, elements.service, elements.maker, elements.code]
    .forEach((element) => { element.value = ""; });
  applyFilters();
});
document.querySelectorAll("th[data-key]").forEach((header) => {
  header.addEventListener("click", () => {
    if (state.sortKey === header.dataset.key) {
      state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = header.dataset.key;
      state.sortDirection = "asc";
    }
    applyFilters();
  });
});

initialize();
