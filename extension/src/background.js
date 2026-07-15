import {
  assertReady,
  loadConfig,
  normalizeConfig,
  saveConfig,
  toPublicConfig,
} from "./config.js";
import {
  disableProxy,
  enableProxy,
  getProxyState,
  isConfiguredProxy,
} from "./proxy-controller.js";

const authAttempts = new Map();
let configPromise = null;
let lastProxyError = null;
let lastTest = null;
let restorePromise = null;

function currentConfig() {
  if (!configPromise) {
    configPromise = loadConfig(chrome.storage.local, async () => {
      const response = await fetch(chrome.runtime.getURL("runtime-config.json"), { cache: "no-store" });
      if (!response.ok) throw new Error("No local bootstrap configuration");
      return response.json();
    });
  }
  return configPromise;
}

async function replaceConfig(next) {
  await saveConfig(chrome.storage.local, next);
  configPromise = Promise.resolve(next);
  return next;
}

function sameProxyChallenge(details, config) {
  if (!details.isProxy || !details.challenger) return false;
  const host = String(details.challenger.host ?? "").replace(/^\[|\]$/g, "").toLowerCase();
  return host === config.host.toLowerCase() && Number(details.challenger.port) === config.port;
}

chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    currentConfig().then((config) => {
      if (!config.enabled || !sameProxyChallenge(details, config)) {
        callback({});
        return;
      }
      const attempt = (authAttempts.get(details.requestId) ?? 0) + 1;
      authAttempts.set(details.requestId, attempt);
      if (attempt > 2) {
        callback({ cancel: true });
        return;
      }
      callback({ authCredentials: { username: config.username, password: config.password } });
    }).catch(() => callback({ cancel: true }));
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["asyncBlocking"],
);

for (const event of [chrome.webRequest.onCompleted, chrome.webRequest.onErrorOccurred]) {
  event.addListener((details) => authAttempts.delete(details.requestId), {
    urls: ["http://*/*", "https://*/*"],
  });
}

chrome.proxy.onProxyError.addListener((details) => {
  lastProxyError = {
    error: String(details.error ?? "UNKNOWN_PROXY_ERROR"),
    details: String(details.details ?? ""),
    fatal: Boolean(details.fatal),
    at: new Date().toISOString(),
  };
});

async function statusPayload() {
  const config = await currentConfig();
  const proxy = await getProxyState(chrome.proxy.settings);
  const active = isConfiguredProxy(proxy, config);
  const conflict = ["not_controllable", "controlled_by_other_extensions"].includes(proxy.levelOfControl);
  return {
    ok: true,
    config: toPublicConfig(config),
    active,
    conflict,
    levelOfControl: proxy.levelOfControl,
    lastProxyError,
    lastTest,
  };
}

async function setEnabled(enabled) {
  let config = await currentConfig();
  if (enabled) {
    assertReady(config);
    await enableProxy(chrome.proxy.settings, config);
    lastProxyError = null;
  } else {
    await disableProxy(chrome.proxy.settings);
  }
  config = await replaceConfig({ ...config, enabled });
  return statusPayload();
}

async function updateConfig(values) {
  const previous = await currentConfig();
  const next = normalizeConfig({ ...values, enabled: previous.enabled }, previous);
  assertReady(next);
  await replaceConfig(next);
  if (next.enabled) await enableProxy(chrome.proxy.settings, next);
  return statusPayload();
}

async function testConnection() {
  const config = await currentConfig();
  assertReady(config);
  if (!config.enabled) throw new Error("请先开启代理");
  const proxy = await getProxyState(chrome.proxy.settings);
  if (!isConfiguredProxy(proxy, config)) throw new Error("插件当前没有控制 Chrome 代理");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  const started = performance.now();
  try {
    const response = await fetch("https://api.ipify.org?format=json", {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`出口检查返回 HTTP ${response.status}`);
    const body = await response.json();
    const ip = String(body.ip ?? "");
    const matchesExpected = !config.expectedIp || ip === config.expectedIp;
    lastTest = {
      ok: matchesExpected,
      ip,
      expectedIp: config.expectedIp,
      latencyMs: Math.round(performance.now() - started),
      at: new Date().toISOString(),
    };
    if (!matchesExpected) throw new Error(`出口 IP 是 ${ip}，不是预期的 ${config.expectedIp}`);
    return statusPayload();
  } finally {
    clearTimeout(timeout);
  }
}

async function handleMessage(message) {
  switch (message?.type) {
    case "GET_STATE": return statusPayload();
    case "SAVE_CONFIG": return updateConfig(message.config ?? {});
    case "SET_ENABLED": return setEnabled(Boolean(message.enabled));
    case "TEST_CONNECTION": return testConnection();
    default: throw new Error("未知的插件操作");
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message)
    .then(sendResponse)
    .catch((error) => sendResponse({ ok: false, error: error?.message ?? String(error) }));
  return true;
});

async function restore() {
  const config = await currentConfig();
  if (config.enabled) {
    try {
      assertReady(config);
      await enableProxy(chrome.proxy.settings, config);
    } catch (error) {
      lastProxyError = {
        error: "RESTORE_FAILED",
        details: error?.message ?? String(error),
        fatal: true,
        at: new Date().toISOString(),
      };
    }
  }
}

function restoreOnce() {
  if (!restorePromise) {
    restorePromise = restore().finally(() => {
      restorePromise = null;
    });
  }
  return restorePromise;
}

chrome.runtime.onInstalled.addListener(() => restoreOnce());
chrome.runtime.onStartup.addListener(() => restoreOnce());
