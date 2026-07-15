export const STORAGE_KEY = "gatewayConfig";

export const DEFAULT_CONFIG = Object.freeze({
  host: "38.207.167.51",
  port: 443,
  username: "",
  password: "",
  expectedIp: "38.207.167.51",
  enabled: false,
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

export function normalizeConfig(value = {}, previous = DEFAULT_CONFIG) {
  const passwordInput = value.password;
  const password = passwordInput === undefined || passwordInput === ""
    ? String(previous.password ?? "")
    : normalizeCredential(passwordInput, "密码", 512);

  return {
    host: normalizeHost(value.host ?? previous.host),
    port: normalizePort(value.port ?? previous.port),
    username: normalizeCredential(value.username ?? previous.username, "用户名", 128),
    password,
    expectedIp: normalizeCredential(value.expectedIp ?? previous.expectedIp, "出口 IP", 253).trim(),
    enabled: Boolean(value.enabled ?? previous.enabled),
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
    hasPassword: Boolean(config.password),
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
