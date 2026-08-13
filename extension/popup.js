const elements = Object.fromEntries([
  "stateBadge", "notice", "settingsForm", "routingMode", "host", "port", "username", "password",
  "expectedIp", "saveButton", "control", "egress", "latency", "toggleButton", "testButton",
  "enrollmentServer", "enrollmentCode", "enrollButton", "dashboardButton", "enrollmentState",
  "syncBridgeButton",
].map((id) => [id, document.getElementById(id)]));

let state = null;
let busy = false;

function message(payload) {
  return chrome.runtime.sendMessage(payload).then((response) => {
    if (!response?.ok) throw new Error(response?.error ?? "插件没有响应");
    return response;
  });
}

function showNotice(text = "", error = false) {
  elements.notice.textContent = text;
  elements.notice.classList.toggle("hidden", !text);
  elements.notice.classList.toggle("error", error);
}

function setBusy(value) {
  busy = value;
  for (const button of [elements.saveButton, elements.toggleButton, elements.testButton, elements.enrollButton, elements.syncBridgeButton]) {
    button.disabled = value;
  }
  elements.dashboardButton.disabled = value || !state?.config.dashboardAvailable;
}

function updateModeForm() {
  const direct = elements.routingMode.value === "direct";
  elements.username.required = !direct;
  elements.password.placeholder = direct
    ? "直连模式不需要服务器密码"
    : state?.config?.hasPassword ? "留空保持现有密码" : "请输入服务器密码";
}

function render(next, populate = false) {
  state = next;
  if (populate) {
    elements.host.value = next.config.host;
    elements.routingMode.value = next.config.routingMode;
    elements.port.value = next.config.port;
    elements.username.value = next.config.username;
    elements.expectedIp.value = next.config.expectedIp;
    updateModeForm();
    elements.enrollmentServer.value = next.config.enrollmentServer || (next.config.host ? `https://${next.config.host}:9443` : "");
  }
  const active = next.active;
  const conflict = next.conflict;
  const modeName = { rule: "规则模式", global: "全局模式", direct: "直连模式" }[next.config.routingMode] ?? "代理";
  elements.stateBadge.textContent = conflict ? "控制冲突" : active ? (next.config.routingMode === "direct" ? "直连中" : "已连接") : next.config.enabled ? "连接异常" : "未开启";
  elements.stateBadge.className = `badge ${conflict || (next.config.enabled && !active) ? "error" : active ? "on" : "off"}`;
  elements.control.textContent = ({
    controlled_by_this_extension: "本插件",
    controllable_by_this_extension: "可接管",
    controlled_by_other_extensions: "其他扩展",
    not_controllable: "不可控制",
  })[next.levelOfControl] ?? next.levelOfControl;
  elements.toggleButton.textContent = next.config.enabled ? `关闭${modeName}` : `开启${modeName}`;
  elements.toggleButton.classList.toggle("danger", next.config.enabled);
  elements.egress.textContent = next.lastTest?.ip ?? (next.config.routingMode === "direct" ? "直连（未检测）" : "尚未检测");
  elements.latency.textContent = next.lastTest ? `${next.lastTest.latencyMs} ms` : "—";
  elements.enrollmentState.textContent = next.config.enrolled
    ? `${next.config.machineName} · 已注册`
    : "尚未注册";
  elements.dashboardButton.disabled = busy || !next.config.dashboardAvailable;
  elements.syncBridgeButton.hidden = !next.config.enrolled;
  if (conflict) showNotice("Chrome 代理由其他扩展控制。请先关闭 FanVPN 或其他代理扩展。", true);
  else if (next.lastProxyError) showNotice(`${next.lastProxyError.error}: ${next.lastProxyError.details}`, true);
  else if (next.config.routingMode === "rule") showNotice("规则模式：中国大陆 IPv4、本地和内网地址直连；其他网站走美国服务器。");
  else if (next.config.routingMode === "direct") showNotice("直连模式不使用 Browser Gateway，也不会沿用系统代理。");
  else showNotice();
}

function formConfig() {
  return {
    host: elements.host.value,
    routingMode: elements.routingMode.value,
    port: Number(elements.port.value),
    username: elements.username.value,
    password: elements.password.value,
    expectedIp: elements.expectedIp.value,
  };
}

async function perform(operation, successText, populate = false) {
  if (busy) return;
  setBusy(true);
  try {
    const next = await operation();
    elements.password.value = "";
    render(next, populate);
    if (successText) showNotice(successText);
  } catch (error) {
    showNotice(error?.message ?? String(error), true);
  } finally {
    setBusy(false);
  }
}

elements.settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  perform(() => message({ type: "SAVE_CONFIG", config: formConfig() }), "设置已保存");
});

elements.routingMode.addEventListener("change", updateModeForm);

elements.toggleButton.addEventListener("click", () => {
  const enable = !state?.config.enabled;
  perform(async () => {
    if (enable) await message({ type: "SAVE_CONFIG", config: formConfig() });
    return message({ type: "SET_ENABLED", enabled: enable });
  }, enable ? "Chrome 已切换到私人美国出口" : "已恢复 Chrome 原有代理设置");
});

elements.testButton.addEventListener("click", () => {
  perform(() => message({ type: "TEST_CONNECTION" }), "出口检测成功");
});

elements.enrollButton.addEventListener("click", () => {
  perform(
    () => message({
      type: "ENROLL_DEVICE",
      server: elements.enrollmentServer.value,
      code: elements.enrollmentCode.value,
    }),
    "设备注册完成，Gateway、用量上报和只读统计均已配置",
    true,
  );
});

elements.dashboardButton.addEventListener("click", () => {
  perform(async () => {
    await message({ type: "OPEN_DASHBOARD" });
    return message({ type: "GET_STATE" });
  });
});

elements.syncBridgeButton.addEventListener("click", () => {
  perform(() => message({ type: "SYNC_BRIDGE_CONFIG" }), "AI Bridge 用量配置已重新同步");
});

setBusy(true);
message({ type: "GET_STATE" })
  .then((next) => render(next, true))
  .catch((error) => showNotice(error?.message ?? String(error), true))
  .finally(() => setBusy(false));
