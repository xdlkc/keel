"use strict";

const state = { refreshing: false, snapshot: null };
const byId = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
  hour12: false, timeZoneName: "short",
});
const reasonLabels = {
  baseline: "首次观察：正在建立基线",
  reset: "管理器或计数器已重置：速率不可用",
  interrupted: "采集曾中断：速率不可用",
  stale: "数据已过期：当前速率不可用",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgElement(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let scaled = value;
  let index = 0;
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  return `${numberFormat.format(scaled)} ${units[index]}`;
}

function formatRate(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  return formatBytes(value);
}

function setTime(node, timestamp) {
  if (!timestamp) {
    node.textContent = "—";
    node.removeAttribute("datetime");
    return;
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    node.textContent = "日期无效";
    node.removeAttribute("datetime");
    return;
  }
  node.dateTime = timestamp;
  node.textContent = dateFormat.format(date);
}

function setNotice(message) {
  const notice = byId("notice");
  notice.hidden = !message;
  notice.textContent = message || "";
}

async function fetchSnapshot(signal) {
  const response = await fetch("/api/traffic?limit=120", {
    cache: "no-store", credentials: "same-origin", signal,
  });
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error("只读服务返回了无法识别的响应。");
  }
  if (!response.ok) throw new Error(payload.message || "无法读取设备流量。");
  return payload;
}

function drawSparkline(container, device) {
  const history = Array.isArray(device.history) ? device.history : [];
  const available = history.filter((point) => (
    point && typeof point.rateBytesPerSecond === "number" &&
    Number.isFinite(point.rateBytesPerSecond) && point.rateBytesPerSecond >= 0
  ));
  if (!available.length) {
    container.appendChild(element("div", "sparkline-empty", "近期没有可绘制的合计速率。"));
    return;
  }

  const width = 360;
  const height = 96;
  const margin = { left: 7, right: 7, top: 10, bottom: 10 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(...available.map((point) => point.rateBytesPerSecond));
  const x = (index) => margin.left + (history.length <= 1 ? plotWidth / 2 : index * plotWidth / (history.length - 1));
  const y = (value) => margin.top + plotHeight - value / Math.max(1, maximum) * plotHeight;
  const svg = svgElement("svg", {
    class: "sparkline", viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `${device.label}近期合计速率折线图，单位字节每秒`,
  });
  svg.appendChild(svgElement("title", {}, `${device.label}近期合计速率`));
  svg.appendChild(svgElement("desc", {}, `最高 ${formatRate(maximum)}/s；断点表示速率不可用。`));
  svg.appendChild(svgElement("line", {
    x1: margin.left, y1: height - margin.bottom, x2: width - margin.right,
    y2: height - margin.bottom, stroke: "#203247", "stroke-width": 1,
  }));

  let segment = [];
  const flush = () => {
    if (segment.length > 1) {
      const path = segment.map((point, index) => (
        `${index ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`
      )).join(" ");
      svg.appendChild(svgElement("path", {
        d: path, fill: "none", stroke: "#38d6c1", "stroke-width": 2,
        "stroke-linecap": "round", "stroke-linejoin": "round",
      }));
    }
    for (const point of segment) {
      const circle = svgElement("circle", {
        cx: point.x, cy: point.y, r: segment.length === 1 ? 3 : 2,
        fill: "#38d6c1", stroke: "#0d1928", "stroke-width": 1,
      });
      circle.appendChild(svgElement("title", {}, `${dateFormat.format(new Date(point.timestamp))} · ${formatRate(point.value)}/s`));
      svg.appendChild(circle);
    }
    segment = [];
  };
  history.forEach((point, index) => {
    if (!point || typeof point.rateBytesPerSecond !== "number" || !Number.isFinite(point.rateBytesPerSecond) || point.rateBytesPerSecond < 0) {
      flush();
    } else {
      segment.push({ x: x(index), y: y(point.rateBytesPerSecond), value: point.rateBytesPerSecond, timestamp: point.timestamp });
    }
  });
  flush();
  container.appendChild(svg);

  const meta = element("div", "sparkline-meta");
  meta.appendChild(element("span", "", `最近 ${history.length} 次采样`));
  meta.appendChild(element("span", "", `峰值 ${formatRate(maximum)}/s`));
  container.appendChild(meta);
  const dates = element("div", "sparkline-dates");
  const firstTime = element("time");
  const lastTime = element("time");
  setTime(firstTime, history[0] && history[0].timestamp);
  setTime(lastTime, history[history.length - 1] && history[history.length - 1].timestamp);
  dates.append(firstTime, lastTime);
  container.appendChild(dates);
}

function renderDevice(device) {
  const card = element("article", `device-card${device.stale ? " is-stale" : ""}${device.error ? " has-error" : ""}`);
  card.setAttribute("aria-labelledby", `device-label-${device.port}`);
  const heading = element("div", "device-heading");
  const titleGroup = element("div");
  titleGroup.appendChild(element("h2", "", device.label));
  titleGroup.firstChild.id = `device-label-${device.port}`;
  titleGroup.appendChild(element("p", "device-port", `服务端端口 ${device.port}`));
  heading.appendChild(titleGroup);
  heading.appendChild(element("span", `transport-badge${device.shared ? " shared" : ""}`, device.shared ? "旧共享传输" : "独立设备端口"));
  card.appendChild(heading);
  if (device.shared) {
    card.appendChild(element("p", "shared-warning", "共享总量不能归因到某台物理设备。"));
  }

  const numbers = element("div", "device-numbers");
  const rateBlock = element("div", "device-number");
  rateBlock.appendChild(element("span", "", "当前合计速率（RX + TX）"));
  const rate = element("strong", "", formatRate(device.rateBytesPerSecond));
  rate.setAttribute("aria-label", device.rateBytesPerSecond === null ? "速率不可用" : `${device.rateBytesPerSecond} 字节每秒`);
  rateBlock.appendChild(rate);
  rateBlock.appendChild(element("span", "unit", device.rateBytesPerSecond === null ? "" : "/s"));
  numbers.appendChild(rateBlock);
  const totalBlock = element("div", "device-number");
  totalBlock.appendChild(element("span", "", "监测以来已跟踪总量"));
  totalBlock.appendChild(element("strong", "", formatBytes(device.totalBytes)));
  if (device.totalBytes !== null) {
    const exact = element("data", "", `${device.totalBytes} B`);
    exact.value = device.totalBytes;
    totalBlock.appendChild(exact);
  }
  numbers.appendChild(totalBlock);
  card.appendChild(numbers);

  const sparkline = element("div", "sparkline-wrap");
  drawSparkline(sparkline, device);
  card.appendChild(sparkline);
  if (device.stale || device.rateUnavailableReason) {
    const reason = device.stale ? "stale" : device.rateUnavailableReason;
    card.appendChild(element("p", `device-state${device.stale ? " stale" : ""}`, reasonLabels[reason] || "当前速率不可用。"));
  }
  if (device.error) card.appendChild(element("p", "device-state error", `采集状态：${device.error}`));
  const observed = element("p", "device-observed", "最后观察：");
  const observedTime = element("time");
  setTime(observedTime, device.observedAt);
  observed.appendChild(observedTime);
  card.appendChild(observed);
  return card;
}

function renderSnapshot(payload) {
  state.snapshot = payload;
  setTime(byId("generated-at"), payload.generatedAt);
  setTime(byId("last-success-at"), payload.collector && payload.collector.lastSuccessAt);
  const staleAfter = payload.collector && payload.collector.staleAfterSeconds;
  byId("stale-after").textContent = typeof staleAfter === "number" ? `${staleAfter} 秒无新样本` : "—";
  const list = byId("device-list");
  list.replaceChildren();
  const devices = Array.isArray(payload.devices) ? payload.devices : [];
  for (const device of devices) list.appendChild(renderDevice(device));
  byId("empty-state").hidden = devices.length > 0;
  byId("empty-state").textContent = devices.length ? "" : "静态映射中没有设备，或尚未有数据。";
  if (payload.collector && payload.collector.error) {
    setNotice(`采集器状态：${payload.collector.error} 显示的数据可能不完整。`);
  } else {
    setNotice(null);
  }
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  byId("refresh-button").disabled = true;
  byId("refresh-label").textContent = "刷新中";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 6000);
  try {
    renderSnapshot(await fetchSnapshot(controller.signal));
  } catch (error) {
    if (state.snapshot) {
      const devices = state.snapshot.devices.map(device => ({
        ...device, rateBytesPerSecond: null, rateUnavailableReason: "interrupted",
      }));
      renderSnapshot({ ...state.snapshot, devices });
    } else {
      byId("empty-state").textContent = "暂时无法读取设备流量。";
    }
    setNotice("与采集服务的连接已中断。");
  } finally {
    window.clearTimeout(timeout);
    state.refreshing = false;
    byId("refresh-button").disabled = false;
    byId("refresh-label").textContent = "刷新数据";
  }
}

byId("refresh-button").addEventListener("click", refresh);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
refresh();
window.setInterval(refresh, 10000);
