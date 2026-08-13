const BYPASS_LIST = Object.freeze([
  "127.0.0.1",
  "localhost",
  "[::1]",
  "<local>",
]);

// PAC cannot ask Chrome for a country code. Rule mode therefore keeps the
// decision local: common domestic suffixes are sent DIRECT, while everything
// else uses the private gateway. Users can still select Global when they want
// every public destination to use the US egress.
const DIRECT_DOMAIN_SUFFIXES = Object.freeze([
  "cn", "中国", "公司", "网络", "公益", "政务", "移动", "我爱你", "在线", "中文网",
  "baidu.com", "bdimg.com", "bcebos.com", "bilibili.com", "bilibili.tv", "alipay.com",
  "taobao.com", "tmall.com", "alicdn.com", "alibaba.com", "aliyun.com", "jd.com",
  "360.cn", "360.com", "so.com", "qq.com", "qpic.cn", "gtimg.com", "weixin.qq.com",
  "weixin.com", "sina.com.cn", "weibo.com", "zhihu.com", "douban.com", "douyin.com",
  "kuaishou.com", "163.com", "126.com", "yeah.net", "netease.com", "meituan.com",
  "dianping.com", "ctrip.com", "c-ctrip.com", "qunar.com", "ifeng.com", "sogou.com",
  "sm.cn", "xiaomi.com", "mi.com", "huawei.com", "hicloud.com", "oppo.com", "vivo.com",
  "bytedance.com", "byteimg.com", "toutiao.com", "csdn.net", "cnblogs.com", "gitee.com",
  "oschina.net", "eastmoney.com", "sse.com.cn", "szse.cn", "gov.cn", "edu.cn",
]);

function pacDomainConditions() {
  return DIRECT_DOMAIN_SUFFIXES
    .map((suffix) => `host === ${JSON.stringify(suffix)} || dnsDomainIs(host, ${JSON.stringify(`.${suffix}`)})`)
    .join(" || ");
}

export function buildRulePac(config) {
  const proxy = `HTTPS ${config.host}:${config.port}`;
  return [
    "function FindProxyForURL(url, host) {",
    "  host = (host || '').toLowerCase();",
    "  if (isPlainHostName(host) || shExpMatch(host, '*.local') || shExpMatch(host, '*.localhost')) return 'DIRECT';",
    "  if (isInNet(host, '127.0.0.0', '255.0.0.0') || isInNet(host, '10.0.0.0', '255.0.0.0') || isInNet(host, '172.16.0.0', '255.240.0.0') || isInNet(host, '192.168.0.0', '255.255.0.0') || isInNet(host, '169.254.0.0', '255.255.0.0')) return 'DIRECT';",
    `  if (${pacDomainConditions()}) return 'DIRECT';`,
    `  return ${JSON.stringify(proxy)};`,
    "}",
  ].join("\n");
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
