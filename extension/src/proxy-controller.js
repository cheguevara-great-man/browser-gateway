import { CHINA_IPV4_RANGES } from "./china-ipv4-ranges.js";

const BYPASS_LIST = Object.freeze([
  "127.0.0.1",
  "localhost",
  "[::1]",
  "<local>",
]);

const rulePacCache = new Map();

export function buildRulePac(config) {
  const cacheKey = `${config.host}:${config.port}`;
  const cached = rulePacCache.get(cacheKey);
  if (cached) return cached;
  const proxy = `HTTPS ${config.host}:${config.port}`;
  const pac = [
    `var CN_IPV4_RANGES = ${JSON.stringify(CHINA_IPV4_RANGES)};`,
    "function ipv4ToNumber(address) {",
    "  var parts = address.split('.');",
    "  if (parts.length !== 4) return -1;",
    "  var value = 0;",
    "  for (var index = 0; index < 4; index += 1) {",
    "    var octet = parseInt(parts[index], 10);",
    "    if (isNaN(octet) || octet < 0 || octet > 255 || String(octet) !== parts[index]) return -1;",
    "    value = value * 256 + octet;",
    "  }",
    "  return value;",
    "}",
    "function shouldDirectIpv4(address) {",
    "  var value = ipv4ToNumber(address);",
    "  if (value < 0) return false;",
    "  if ((value >= 2130706432 && value <= 2147483647) || (value >= 167772160 && value <= 184549375) || (value >= 2886729728 && value <= 2887778303) || (value >= 3232235520 && value <= 3232301055) || (value >= 2851995648 && value <= 2852061183)) return true;",
    "  var low = 0; var high = CN_IPV4_RANGES.length - 1;",
    "  while (low <= high) {",
    "    var middle = Math.floor((low + high) / 2); var range = CN_IPV4_RANGES[middle];",
    "    if (value < range[0]) high = middle - 1;",
    "    else if (value > range[1]) low = middle + 1;",
    "    else return true;",
    "  }",
    "  return false;",
    "}",
    "function FindProxyForURL(url, host) {",
    "  host = (host || '').toLowerCase();",
    "  if (isPlainHostName(host) || shExpMatch(host, '*.local') || shExpMatch(host, '*.localhost')) return 'DIRECT';",
    "  var address = dnsResolve(host);",
    "  if (address && shouldDirectIpv4(address)) return 'DIRECT';",
    `  return ${JSON.stringify(proxy)};`,
    "}",
  ].join("\n");
  rulePacCache.set(cacheKey, pac);
  return pac;
}

function invokeChromeSetting(method, details) {
  return new Promise((resolve, reject) => {
    method(details, () => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

export function buildProxyValue(config) {
  if (config.routingMode === "direct") return { mode: "direct" };
  if (config.routingMode === "rule") {
    return {
      mode: "pac_script",
      pacScript: { data: buildRulePac(config), mandatory: true },
    };
  }
  return {
    mode: "fixed_servers",
    rules: {
      singleProxy: {
        scheme: "https",
        host: config.host,
        port: config.port,
      },
      bypassList: [...BYPASS_LIST],
    },
  };
}

export function getProxyState(proxySettings) {
  return new Promise((resolve, reject) => {
    proxySettings.get({ incognito: false }, (details) => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve(details);
    });
  });
}

export async function enableProxy(proxySettings, config) {
  const before = await getProxyState(proxySettings);
  if (["not_controllable", "controlled_by_other_extensions"].includes(before.levelOfControl)) {
    throw new Error("Chrome 代理正由 Clash 扩展、FanVPN 或其他扩展控制");
  }
  await invokeChromeSetting(proxySettings.set.bind(proxySettings), {
    value: buildProxyValue(config),
    scope: "regular",
  });
  const after = await getProxyState(proxySettings);
  if (after.levelOfControl !== "controlled_by_this_extension") {
    throw new Error("插件未能取得 Chrome 代理控制权");
  }
  return after;
}

export async function disableProxy(proxySettings) {
  const current = await getProxyState(proxySettings);
  if (current.levelOfControl === "controlled_by_this_extension") {
    await invokeChromeSetting(proxySettings.clear.bind(proxySettings), { scope: "regular" });
  }
  return getProxyState(proxySettings);
}

export function isConfiguredProxy(details, config) {
  if (config.routingMode === "direct") {
    return details?.levelOfControl === "controlled_by_this_extension"
      && details?.value?.mode === "direct";
  }
  if (config.routingMode === "rule") {
    return details?.levelOfControl === "controlled_by_this_extension"
      && details?.value?.mode === "pac_script"
      && details?.value?.pacScript?.data === buildRulePac(config);
  }
  const proxy = details?.value?.rules?.singleProxy;
  return details?.levelOfControl === "controlled_by_this_extension"
    && details?.value?.mode === "fixed_servers"
    && proxy?.scheme === "https"
    && String(proxy?.host ?? "").toLowerCase() === config.host.toLowerCase()
    && Number(proxy?.port) === config.port;
}
