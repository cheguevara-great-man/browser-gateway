const BYPASS_LIST = Object.freeze([
  "127.0.0.1",
  "localhost",
  "[::1]",
  "<local>",
]);

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
  const proxy = details?.value?.rules?.singleProxy;
  return details?.levelOfControl === "controlled_by_this_extension"
    && details?.value?.mode === "fixed_servers"
    && proxy?.scheme === "https"
    && String(proxy?.host ?? "").toLowerCase() === config.host.toLowerCase()
    && Number(proxy?.port) === config.port;
}

\n