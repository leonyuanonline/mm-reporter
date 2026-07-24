const state = {
  manifest: null,
  rows: [],
  filteredRows: [],
  sortKey: "证券代码",
  sortDirection: "asc",
  reportBase: "./reports",
  reportAvailable: false,
};

const elements = {
  date: document.querySelector("#report-date"),
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
    elements.date.value = target.date;
    loadReport(target.date);
  }
}

async function initialize() {
  try {
    state.manifest = await fetchManifest();
    if (!Array.isArray(state.manifest.reports) || state.manifest.reports.length === 0) {
      throw new Error("目前还没有可显示的历史报告。");
    }
    const availableDates = state.manifest.reports.map((report) => report.date);
    elements.date.min = availableDates[availableDates.length - 1];
    elements.date.max = availableDates[0];
    elements.latestDate.textContent = `最新报告：${state.manifest.latest}`;
    const params = new URLSearchParams(location.search);
    const requestedDate = params.get("date");
    const initialDate = state.manifest.reports.some((report) => report.date === requestedDate)
      ? requestedDate
      : state.manifest.latest;
    elements.date.value = initialDate;
    await loadReport(initialDate);
    restoreFilters(params);
    applyFilters();
  } catch (error) {
    elements.visibleCount.textContent = "!";
    elements.summaryLabel.textContent = "载入失败";
    elements.message.textContent = error.message;
  }
}

elements.date.addEventListener("change", () => loadReport(elements.date.value));
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
