import test from "node:test";
import assert from "node:assert/strict";
import {
  buildProxyValue,
  buildRulePac,
  enableProxy,
  isConfiguredProxy,
} from "../src/proxy-controller.js";

const config = { host: "38.207.167.51", port: 443 };

test("builds an HTTPS proxy while bypassing loopback", () => {
  const value = buildProxyValue(config);
  assert.deepEqual(value.rules.singleProxy, { scheme: "https", host: "38.207.167.51", port: 443 });
  assert.ok(value.rules.bypassList.includes("127.0.0.1"));
  assert.ok(value.rules.bypassList.includes("localhost"));
});

test("builds a local PAC that resolves and binary-searches Chinese IPv4 ranges", () => {
  const config = { host: "38.207.167.51", port: 443, routingMode: "rule" };
  const value = buildProxyValue(config);
  assert.equal(value.mode, "pac_script");
  assert.equal(value.pacScript.data, buildRulePac(config));
  assert.match(value.pacScript.data, /var CN_IPV4_RANGES = \[\[/);
  assert.match(value.pacScript.data, /var address = dnsResolve\(host\)/);
  assert.match(value.pacScript.data, /while \(low <= high\)/);
  assert.match(value.pacScript.data, /HTTPS 38\.207\.167\.51:443/);
  assert.equal(isConfiguredProxy({
    levelOfControl: "controlled_by_this_extension",
    value,
  }, config), true);

  const findProxyFor = new Function("dnsResolve", "isPlainHostName", "shExpMatch", `${value.pacScript.data}\nreturn FindProxyForURL;`)(
    (host) => ({ "www.baidu.com": "110.242.68.66", "dns.google": "8.8.8.8" }[host]),
    (host) => !host.includes("."),
    (host, pattern) => pattern.startsWith("*.") && host.endsWith(pattern.slice(1)),
  );
  assert.equal(findProxyFor("https://www.baidu.com", "www.baidu.com"), "DIRECT");
  assert.equal(findProxyFor("https://dns.google", "dns.google"), "HTTPS 38.207.167.51:443");
});

test("uses Chrome direct mode without contacting the gateway", () => {
  const config = { host: "38.207.167.51", port: 443, routingMode: "direct" };
  const value = buildProxyValue(config);
  assert.deepEqual(value, { mode: "direct" });
  assert.equal(isConfiguredProxy({
    levelOfControl: "controlled_by_this_extension",
    value,
  }, config), true);
});

test("recognizes only this exact active proxy", () => {
  const details = { levelOfControl: "controlled_by_this_extension", value: buildProxyValue(config) };
  assert.equal(isConfiguredProxy(details, config), true);
  assert.equal(isConfiguredProxy(details, { host: "other.example", port: 443 }), false);
});

test("refuses to overwrite another extension proxy", async () => {
  globalThis.chrome = { runtime: {} };
  const settings = {
    get(_details, callback) { callback({ levelOfControl: "controlled_by_other_extensions", value: {} }); },
    set() { assert.fail("set must not be called"); },
  };
  await assert.rejects(() => enableProxy(settings, config), /其他扩展控制/);
});
