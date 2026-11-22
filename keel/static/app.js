"use strict";

const state = {
  csrfToken: null,
  target: null,
  statusLoaded: false,
  status: "UNKNOWN",
  action: null,
  actionTarget: null,
  actionPending: false,
  refreshing: false,
};

const byId = (id) => document.getElementById(id);
const statusLabels = {
  PROVISIONING: "准备中",
  STAGING: "启动中",
  RUNNING: "运行中",
  STOPPING: "停止中",
  TERMINATED: "已停止",
  SUSPENDING: "暂停中",
  SUSPENDED: "已暂停",
  REPAIRING: "修复中",
  UNKNOWN: "状态未知",
};

function setNotice(message, type = "info") {
  const notice = byId("notice");
  if (!message) {
    notice.hidden = true;
    notice.textContent = "";
    return;
  }
  notice.textContent = message;
  notice.className = `notice${type === "error" ? " notice-error" : type === "success" ? " notice-success" : ""}`;
  notice.hidden = false;
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
  });
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error("本地服务返回了无法识别的响应。");
  }
  if (!response.ok) {
    const failure = new Error(payload.message || "请求失败，请稍后重试。");
    failure.code = payload.error || "request";
    throw failure;
  }
  return payload;
}

function renderDashboardLink(data) {
  byId("dashboard-link").hidden = !(data.links && data.links.dashboard);
}

async function ensureSession() {
  if (!state.csrfToken) {
    const session = await fetchJson("/api/session");
    state.csrfToken = session.csrfToken;
    renderDashboardLink(session);
  }
}

function formatTime(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function updateActionButtons() {
  const unavailable = !state.statusLoaded || state.actionPending || state.refreshing;
  byId("start-button").disabled = unavailable || state.status !== "TERMINATED";
  byId("stop-button").disabled = unavailable || state.status !== "RUNNING";
}

function renderStatus(data) {
  state.target = {
    project: data.project,
    zone: data.zone,
    instance: data.instance,
  };
  state.statusLoaded = true;
  state.status = data.status || "UNKNOWN";
  byId("instance-name").textContent = data.name || data.instance;
  byId("project-name").textContent = data.project || "—";
  byId("zone-value").textContent = data.zone || "—";
  byId("machine-type-value").textContent = data.machineType || "未知";
  byId("boot-disk-value").textContent = data.bootDiskSizeGb ? `${data.bootDiskSizeGb} GB` : "未知";
  byId("public-ip-value").textContent = data.publicIp || "未配置";
  byId("updated-at-value").textContent = formatTime(data.updatedAt);
  byId("status-text").textContent = statusLabels[state.status] || state.status;
  renderDashboardLink(data);

  const pill = byId("status-pill");
  let statusClass = "status-unknown";
  if (state.status === "RUNNING") statusClass = "status-running";
  else if (["TERMINATED", "SUSPENDED"].includes(state.status)) statusClass = "status-stopped";
  else if (["PROVISIONING", "STAGING", "STOPPING", "SUSPENDING", "REPAIRING"].includes(state.status)) statusClass = "status-pending";
  pill.className = `status-pill ${statusClass}`;
  updateActionButtons();
}

function renderStatusError() {
  state.target = null;
  state.statusLoaded = false;
  state.status = "UNKNOWN";
  byId("instance-name").textContent = "实例不可用";
  for (const id of ["project-name", "zone-value", "machine-type-value", "boot-disk-value", "public-ip-value", "updated-at-value"]) {
    byId(id).textContent = "—";
  }
  byId("status-text").textContent = "状态未知";
  byId("status-pill").className = "status-pill status-unknown";
  updateActionButtons();
}

async function refreshAll(options = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  updateActionButtons();
  byId("refresh-button").disabled = true;
  byId("refresh-button").classList.add("is-refreshing");
  byId("refresh-label").textContent = "刷新中";
  if (!options.preserveNotice) setNotice(null);

  try {
    await ensureSession();
    renderStatus(await fetchJson("/api/status"));
  } catch (error) {
    renderStatusError();
    const prefix = options.preserveNotice ? "gcloud 操作命令已结束，但刷新失败：" : "";
    setNotice(prefix + error.message, "error");
  } finally {
    state.refreshing = false;
    byId("refresh-button").disabled = false;
    byId("refresh-button").classList.remove("is-refreshing");
    byId("refresh-label").textContent = "刷新数据";
    updateActionButtons();
  }
}

function openConfirmation(action) {
  const target = state.target;
  if (!target || !target.project || !target.zone || !target.instance || state.actionPending || state.refreshing) return;
  if ((action === "start" && state.status !== "TERMINATED") || (action === "stop" && state.status !== "RUNNING")) return;
  state.action = action;
  state.actionTarget = { ...target };
  byId("dialog-title").textContent = action === "start" ? "确认启动实例" : "确认停止实例";
  byId("dialog-description").textContent = action === "start"
    ? "启动实例可能产生计算费用。请确认你选择了正确的实例。"
    : "停止实例会中断工作；持久磁盘和静态或保留公网 IP 仍可能计费，临时外部 IPv4 地址可能在再次启动后改变。";
  byId("dialog-target").textContent = `项目 ${target.project} · 可用区 ${target.zone}`;
  byId("dialog-instance").textContent = target.instance;
  byId("confirm-action").textContent = action === "start" ? "确认启动" : "确认停止";
  byId("confirm-action").className = `button ${action === "start" ? "button-primary" : "button-danger"}`;
  byId("confirm-input").value = "";
  byId("confirm-action").disabled = true;
  setNotice(null);
  const dialog = byId("confirm-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  byId("confirm-input").focus();
}

function closeConfirmation(force = false) {
  if (state.actionPending && !force) return;
  const dialog = byId("confirm-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  state.action = null;
  state.actionTarget = null;
}

async function submitAction(event) {
  event.preventDefault();
  const action = state.action;
  const target = state.actionTarget;
  const confirmation = byId("confirm-input").value;
  if (!target || !action || confirmation !== target.instance || state.actionPending) return;
  state.actionPending = true;
  updateActionButtons();
  byId("confirm-input").disabled = true;
  byId("cancel-action").disabled = true;
  byId("confirm-action").disabled = true;
  byId("confirm-action").textContent = "等待 gcloud 完成…";
  setNotice("正在等待 gcloud 完成操作，尚未确认最终实例状态。", "info");

  try {
    await ensureSession();
    const result = await fetchJson(`/api/actions/${action}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Keel-CSRF": state.csrfToken,
      },
      body: JSON.stringify({
        project: target.project,
        zone: target.zone,
        instance: target.instance,
        confirmation,
      }),
    });
    closeConfirmation(true);
    setNotice(result.message, "success");
    await refreshAll({ preserveNotice: true });
  } catch (error) {
    if (error.code === "csrf") state.csrfToken = null;
    closeConfirmation(true);
    setNotice(error.message, "error");
  } finally {
    state.actionPending = false;
    byId("confirm-input").disabled = false;
    byId("cancel-action").disabled = false;
    byId("confirm-input").value = "";
    byId("confirm-action").textContent = action === "start" ? "确认启动" : "确认停止";
    byId("confirm-action").disabled = true;
    updateActionButtons();
  }
}

byId("refresh-button").addEventListener("click", () => refreshAll());
byId("start-button").addEventListener("click", () => openConfirmation("start"));
byId("stop-button").addEventListener("click", () => openConfirmation("stop"));
byId("cancel-action").addEventListener("click", () => closeConfirmation());
byId("confirm-form").addEventListener("submit", submitAction);
byId("confirm-input").addEventListener("input", (event) => {
  byId("confirm-action").disabled = state.actionPending || !state.actionTarget || event.target.value !== state.actionTarget.instance;
});
byId("confirm-dialog").addEventListener("cancel", (event) => {
  if (state.actionPending) event.preventDefault();
});

refreshAll();
