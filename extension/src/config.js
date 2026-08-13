export const STORAGE_KEY = "gatewayConfig";
export const ROUTING_MODES = Object.freeze(["rule", "global", "direct"]);

export const DEFAULT_CONFIG = Object.freeze({
  host: "38.207.167.51",
  port: 443,
  username: "",
  password: "",
  expectedIp: "38.207.167.51",
  enabled: false,
  routingMode: "rule",
  enrollmentServer: "",
  machineId: "",
  machineName: "",
  deviceToken: "",
  usageCollectorUrl: "",
  dashboardUrl: "",
});

function normalizeHost(value) {
  const host = String(value ?? "").trim().toLowerCase();
  if (!host || host.length > 253 || /[\s/@?#]/.test(host)) {
    throw new Error("服务器地址格式不正确");
  }
  let parsed;
  try {
    parsed = new URL(`https://${host}/`);
  } catch {
    throw new Error("服务器地址格式不正确");
  }
  if (!parsed.hostname || parsed.port || parsed.username || parsed.password) {
    throw new Error("服务器地址只能填写 IP 或域名");
  }
  return parsed.hostname.replace(/^\[|\]$/g, "");
}

function normalizePort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("端口必须是 1 到 65535 之间的整数");
  }
  return port;
}

function normalizeCredential(value, label, maximum) {
  const result = String(value ?? "");
  if (result.length > maximum || /[\r\n\0]/.test(result)) {
    throw new Error(`${label}格式不正确`);
  }
  return result;
}

function normalizeRoutingMode(value, fallback) {
  const mode = String(value ?? fallback ?? "rule").trim().toLowerCase();
  if (!ROUTING_MODES.includes(mode)) {
    throw new Error("代理模式必须是规则模式、全局模式或直连模式");
  }
  return mode;
}

export function normalizeConfig(value = {}, previous = DEFAULT_CONFIG) {
  const passwordInput = value.password;
  const password = passwordInput === undefined || passwordInput === ""
    ? String(previous.password ?? "")
    : normalizeCredential(passwordInput, "密码", 512);

  // Older installations used a fixed proxy and therefore behaved as global
  // mode. Preserve that behavior during the one-time storage migration.
  const routingMode = value.routingMode === undefined
    ? (Object.hasOwn(value, "enabled") && Boolean(value.enabled) ? "global" : previous.routingMode)
    : value.routingMode;

  return {
    host: normalizeHost(value.host ?? previous.host),
    port: normalizePort(value.port ?? previous.port),
    username: normalizeCredential(value.username ?? previous.username, "用户名", 128),
    password,
    expectedIp: normalizeCredential(value.expectedIp ?? previous.expectedIp, "出口 IP", 253).trim(),
    enabled: Boolean(value.enabled ?? previous.enabled),
    routingMode: normalizeRoutingMode(routingMode, previous.routingMode),
    enrollmentServer: normalizeCredential(value.enrollmentServer ?? previous.enrollmentServer, "注册服务器", 512).trim().replace(/\/$/, ""),
    machineId: normalizeCredential(value.machineId ?? previous.machineId, "设备 ID", 64),
    machineName: normalizeCredential(value.machineName ?? previous.machineName, "设备名称", 128),
    deviceToken: normalizeCredential(value.deviceToken ?? previous.deviceToken, "设备 Token", 512),
    usageCollectorUrl: normalizeCredential(value.usageCollectorUrl ?? previous.usageCollectorUrl, "用量地址", 1024),
    dashboardUrl: normalizeCredential(value.dashboardUrl ?? previous.dashboardUrl, "统计网页", 1024),
  };
}

export function assertReady(config) {
  if (!config.username || !config.password) {
    throw new Error("请先填写服务器生成的用户名和密码");
  }
}

export function toPublicConfig(config) {
  return {
    host: config.host,
    port: config.port,
    username: config.username,
    expectedIp: config.expectedIp,
    enabled: config.enabled,
    routingMode: config.routingMode,
    hasPassword: Boolean(config.password),
    enrollmentServer: config.enrollmentServer,
    machineId: config.machineId,
    machineName: config.machineName,
    enrolled: Boolean(config.machineId && config.deviceToken),
    dashboardAvailable: Boolean(config.dashboardUrl && config.deviceToken),
  };
}

export async function loadConfig(storageArea, bootstrapLoader = null) {
  const stored = await storageArea.get(STORAGE_KEY);
  if (stored[STORAGE_KEY]) return normalizeConfig(stored[STORAGE_KEY], DEFAULT_CONFIG);
  if (bootstrapLoader) {
    try {
      const bootstrapped = normalizeConfig(await bootstrapLoader(), DEFAULT_CONFIG);
      await storageArea.set({ [STORAGE_KEY]: bootstrapped });
      return bootstrapped;
    } catch {
      // A repository checkout intentionally has no runtime-config.json.
    }
  }
  return normalizeConfig({}, DEFAULT_CONFIG);
}

export async function saveConfig(storageArea, config) {
  await storageArea.set({ [STORAGE_KEY]: config });
}
