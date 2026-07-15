import test from "node:test";
import assert from "node:assert/strict";
import { buildProxyValue, enableProxy, isConfiguredProxy } from "../src/proxy-controller.js";

const config = { host: "38.207.167.51", port: 443 };

test("builds an HTTPS proxy while bypassing loopback", () => {
  const value = buildProxyValue(config);
  assert.deepEqual(value.rules.singleProxy, { scheme: "https", host: "38.207.167.51", port: 443 });
  assert.ok(value.rules.bypassList.includes("127.0.0.1"));
  assert.ok(value.rules.bypassList.includes("localhost"));
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
