const elements = Object.fromEntries([
  "stateBadge", "notice", "settingsForm", "host", "port", "username", "password",
  "expectedIp", "saveButton", "control", "egress", "latency", "toggleButton", "testButton",
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
  for (const button of [elements.saveButton, elements.toggleButton, elements.testButton]) {
    button.disabled = value;
  }
}

function render(next, populate = false) {
  state = next;
  if (populate) {
    elements.host.value = next.config.host;
    elements.port.value = next.config.port;
    elements.username.value = next.config.username;
    elements.expectedIp.value = next.config.expectedIp;
    elements.password.placeholder = next.config.hasPassword ? "留空保持现有密码" : "请输入服务器密码";
  }
  const active = next.active;
  const conflict = next.conflict;
  elements.stateBadge.textContent = conflict ? "控制冲突" : active ? "已连接" : next.config.enabled ? "连接异常" : "未开启";
  elements.stateBadge.className = `badge ${conflict || (next.config.enabled && !active) ? "error" : active ? "on" : "off"}`;
  elements.control.textContent = ({
    controlled_by_this_extension: "本插件",
    controllable_by_this_extension: "可接管",
    controlled_by_other_extensions: "其他扩展",
    not_controllable: "不可控制",
  })[next.levelOfControl] ?? next.levelOfControl;
  elements.toggleButton.textContent = next.config.enabled ? "关闭代理" : "开启代理";
  elements.toggleButton.classList.toggle("danger", next.config.enabled);
  elements.egress.textContent = next.lastTest?.ip ?? "尚未检测";
  elements.latency.textContent = next.lastTest ? `${next.lastTest.latencyMs} ms` : "—";
  if (conflict) showNotice("Chrome 代理由其他扩展控制。请先关闭 FanVPN 或其他代理扩展。", true);
  else if (next.lastProxyError) showNotice(`${next.lastProxyError.error}: ${next.lastProxyError.details}`, true);
  else showNotice();
}

function formConfig() {
  return {
    host: elements.host.value,
    port: Number(elements.port.value),
    username: elements.username.value,
    password: elements.password.value,
    expectedIp: elements.expectedIp.value,
  };
}

async function perform(operation, successText) {
  if (busy) return;
  setBusy(true);
  try {
    const next = await operation();
    elements.password.value = "";
    render(next);
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

setBusy(true);
message({ type: "GET_STATE" })
  .then((next) => render(next, true))
  .catch((error) => showNotice(error?.message ?? String(error), true))
  .finally(() => setBusy(false));
