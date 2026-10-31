import { resolveLocale, translateFor } from "@openbot/i18n";

const locale = resolveLocale("system", new URLSearchParams(location.search).get("lang") || "zh-CN");
const t = translateFor(locale);
document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
for (const element of document.querySelectorAll("[data-i18n]")) element.textContent = t(element.dataset.i18n);
for (const element of document.querySelectorAll("[data-i18n-aria]"))
  element.setAttribute("aria-label", t(element.dataset.i18nAria));
const $ = (selector) => document.querySelector(selector);
const safe = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const fmt = (value, digits = 2) =>
  value == null
    ? t("orgHealthDemo.notProvided")
    : Number(value).toLocaleString(locale === "zh" ? "zh-CN" : "en-US", {
        minimumFractionDigits: 0,
        maximumFractionDigits: digits,
      });
const valueFor = (series, label) => series?.[label] ?? null;
function normalizeSnapshot(data) {
  if (data?.scope) return data;
  if (!data?.范围 || !Array.isArray(data?.指标)) return data;
  return {
    scope: data.范围,
    conclusion: data.结论,
    origin: data.范围["来源"],
    metrics: data.指标.slice(0, 4),
    stage: data.指标[4],
    insights: data.洞察,
    definitions: data.来源口径,
    risks: data.风险,
    recommendations: data.建议,
  };
}
function metricCard(title, unit, series, delta, options = {}) {
  const points = [
    ["2026-01实际", t("orgHealthDemo.actual01"), "actual"],
    ["2026-05H1参考", t("orgHealthDemo.reference05"), "frozen"],
    ["2026-09实际", t("orgHealthDemo.actual09"), "actual"],
    [options.forecastKey || "年末测算", options.forecastLabel || t("orgHealthDemo.forecast"), "forecast"],
  ];
  // May is retained as an explicit unavailable anchor; forecast is visually separated.
  return `<article class="metric-card"><div class="metric-name">${title}</div><div class="metric-unit">${unit}</div><div class="metric-values">${points
    .map(([key, label, type]) => {
      const v = valueFor(series, key);
      const missing = v == null;
      return `<div><div class="metric-value-label">${label}</div><div class="metric-value ${type === "forecast" ? "forecast" : ""} ${missing ? "unavailable" : ""}">${missing ? t("orgHealthDemo.notProvided") : safe(options.format ? options.format(v) : fmt(v, options.digits ?? 2))}</div></div>`;
    })
    .join("")}</div><div class="metric-delta ${options.warn ? "warn" : ""}">${delta}</div></article>`;
}
function render(data) {
  const expected = ["整体技术人力成本", "整体折算人头", "营收业务（含共享平台）技术人力成本与折算人头", "收入与成本率"];
  if (
    data.scope?.["schema版本"] !== 4 ||
    data.scope?.["观察月份"] !== "2026-09" ||
    !data.scope?.["数据截止"] ||
    expected.some((title, i) => data.metrics?.[i]?.["标题"] !== title) ||
    !["insights", "definitions", "risks", "recommendations"].every((key) => Array.isArray(data[key]))
  ) {
    throw new Error(t("orgHealthDemo.badStructure"));
  }
  const scope = data.scope;
  $("#conclusion").textContent = data.conclusion;
  $("#asof-short").textContent = t("orgHealthDemo.asOf", { date: scope["数据截止"] });
  $("#scope-month").textContent = scope["观察月份"];
  $("#scope-date").textContent = scope["数据截止"];
  $("#scope-source").textContent = `${scope["来源"]} · schema v${scope["schema版本"]}`;
  $("#footer-source").textContent = t("orgHealthDemo.sourceLine", { origin: data.origin });
  if (Array.isArray(data.stage?.指标)) {
    $("#stage-title").textContent = data.stage["标题"];
    $("#stage-note").textContent = data.stage["来源口径"];
    $("#stage-list").innerHTML = data.stage.指标
      .map(
        (item, index) =>
          `<div><span class="stage-index">${String.fromCharCode(65 + index)}</span><b>${safe(item["分档"])}</b><p>${safe(item["例证"])}</p></div>`,
      )
      .join("");
  }
  const m = data.metrics;
  const overallCost = m[0].数值;
  const overallHeads = m[1].数值;
  const revenueOps = m[2].数值;
  const revenue = m[3].数值["收入万美元每月"];
  const costRates = m[3].数值["整体技术人力成本率"];
  const revRates = m[3].数值["营收业务技术人力成本率"];
  const fullRates = m[3].数值["全口径技术成本率含运维设施"];
  const numericSeries = [overallCost, overallHeads, revenueOps?.成本, revenueOps?.折算人头, revenue];
  const dates = ["2026-01实际", "2026-05H1参考", "2026-09实际"];
  if (
    numericSeries.some(
      (series, i) =>
        !series ||
        [...dates, i === 4 ? "年末目标" : "年末测算"].some(
          (key) =>
            !(key in series) ||
            (series[key] !== null && (typeof series[key] !== "number" || !Number.isFinite(series[key]))),
        ),
    ) ||
    overallCost["2026-05H1参考"] !== null ||
    overallHeads["2026-05H1参考"] !== null ||
    !costRates ||
    !revRates ||
    !fullRates
  ) {
    throw new Error(t("orgHealthDemo.badValues"));
  }
  const asPercent = (x) => (x == null ? t("orgHealthDemo.notProvided") : String(x));
  const difference = (series, digits = 2) => {
    const start = series["2026-01实际"],
      end = series["2026-09实际"];
    if (start == null || end == null) return t("orgHealthDemo.notProvided");
    const change = Number(end) - Number(start);
    return `${change > 0 ? "+" : ""}${fmt(change, digits)}`;
  };
  const percentChange = (series) => {
    const start = series["2026-01实际"],
      end = series["2026-09实际"];
    if (start == null || end == null || !Number(start)) return t("orgHealthDemo.notProvided");
    const change = (Number(end) / Number(start) - 1) * 100;
    return `${change > 0 ? "+" : ""}${fmt(change, 1)}%`;
  };
  $("#metric-grid").innerHTML = [
    metricCard(
      t("orgHealthDemo.cost"),
      t("orgHealthDemo.costUnit"),
      overallCost,
      `${t("orgHealthDemo.change")} <strong>${difference(overallCost, 1)} ${t("orgHealthDemo.moneyUnit")} · ${percentChange(overallCost)}</strong>`,
      { digits: 1 },
    ),
    metricCard(
      t("orgHealthDemo.headcount"),
      t("orgHealthDemo.headUnit"),
      overallHeads,
      `${t("orgHealthDemo.change")} <strong>${difference(overallHeads, 1)} · ${percentChange(overallHeads)}</strong>`,
      { digits: 1 },
    ),
    metricCard(
      t("orgHealthDemo.revenueCost"),
      t("orgHealthDemo.revenueCostUnit"),
      revenueOps.成本,
      `${t("orgHealthDemo.change")} <strong>${difference(revenueOps.成本, 1)} ${t("orgHealthDemo.moneyUnit")}</strong>`,
      { digits: 1 },
    ),
    metricCard(
      t("orgHealthDemo.revenueHeadcount"),
      t("orgHealthDemo.revenueHeadUnit"),
      revenueOps.折算人头,
      `${t("orgHealthDemo.change")} <strong>${difference(revenueOps.折算人头, 1)}</strong>`,
      { digits: 1 },
    ),
    metricCard(
      t("orgHealthDemo.revenue"),
      t("orgHealthDemo.revenueUnit"),
      revenue,
      `${t("orgHealthDemo.revenueChange")} <strong>${percentChange(revenue)}</strong> · ${t("orgHealthDemo.yearEndRevenue")}`,
      { digits: 0, forecastKey: "年末目标", forecastLabel: t("orgHealthDemo.forecastTarget") },
    ),
    metricCard(
      t("orgHealthDemo.costRatio"),
      t("orgHealthDemo.costRatioUnit"),
      costRates,
      safe(
        t("orgHealthDemo.ratioSummary", {
          a: asPercent(revRates["2026-01实际"]),
          b: asPercent(revRates["2026-09实际"]),
          c: asPercent(fullRates["整体2026-01实际"]),
          d: asPercent(fullRates["整体2026-09实际"]),
        }),
      ),
      { format: asPercent, forecastLabel: t("orgHealthDemo.forecastRevenue"), warn: true },
    ),
  ].join("");
  $("#insight-grid").innerHTML = data.insights
    .map((item) => {
      const type = String(item["类型"] ?? "");
      const tagClass = type.includes("推断")
        ? "inference"
        : type.includes("边界")
          ? "boundary"
          : type.includes("事实")
            ? "fact"
            : "inference";
      const label = type.includes("推断")
        ? t("orgHealthDemo.inference")
        : type.includes("边界")
          ? t("orgHealthDemo.boundary")
          : type.includes("事实")
            ? t("orgHealthDemo.fact")
            : t("orgHealthDemo.unknown");
      return `<article class="insight-card"><span class="tag ${tagClass}">${label}</span><h3>${safe(item["标题"])}</h3><p>${safe(item["内容"])}</p></article>`;
    })
    .join("");
  $("#definitions-grid").innerHTML = data.definitions
    .map(
      (text, index) =>
        `<div class="definition"><span class="definition-no">${String(index + 1).padStart(2, "0")}</span><p>${safe(text)}</p></div>`,
    )
    .join("");
  $("#risk-grid").innerHTML = data.risks
    .map(
      (item) =>
        `<article class="risk-card"><h3><span class="risk-mark">!</span>${safe(item["标题"])}</h3><p>${safe(item["内容"])}</p><span class="missing">${t("orgHealthDemo.missingEvidence")}</span></article>`,
    )
    .join("");
  $("#recommendations").innerHTML = data.recommendations
    .map(
      (item) =>
        `<article class="recommendation"><div><span class="priority ${safe(item["优先级"])}">${safe(item["优先级"])}</span></div><div><h3>${safe(item["标题"])}</h3><span class="rec-label">${t("orgHealthDemo.recommendation")}</span></div><div><span class="rec-label">${t("orgHealthDemo.basis")}</span><p>${safe(item["依据"])} ${safe(item["缺失证据"])}</p></div><div><span class="rec-label">${t("orgHealthDemo.verify")}</span><p>${safe(item["验证方式"])}</p></div></article>`,
    )
    .join("");
}
fetch("./data/diagnosis.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(t("orgHealthDemo.localLoadFailed"));
    return response.json();
  })
  .then(normalizeSnapshot)
  .then(render)
  .catch((error) => {
    ["metric-grid", "insight-grid", "definitions-grid", "risk-grid", "recommendations"].forEach((id) => {
      document.getElementById(id).replaceChildren();
    });
    ["scope-month", "scope-date", "scope-source", "footer-source"].forEach((id) => {
      document.getElementById(id).textContent = "—";
    });
    $("#asof-short").textContent = t("orgHealthDemo.loadFailed");
    $("#conclusion").textContent = error.message || t("orgHealthDemo.genericError");
  });
