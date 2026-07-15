import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_CONFIG, assertReady, normalizeConfig, toPublicConfig } from "../src/config.js";

test("normalizes a valid gateway configuration", () => {
  const config = normalizeConfig({
    host: " 38.207.167.51 ", port: "443", username: "alice", password: "secret", enabled: true,
  });
  assert.equal(config.host, "38.207.167.51");
  assert.equal(config.port, 443);
  assert.equal(config.enabled, true);
  assertReady(config);
});

test("preserves an existing password when the form leaves it blank", () => {
  const previous = { ...DEFAULT_CONFIG, username: "alice", password: "existing" };
  const config = normalizeConfig({ host: "proxy.example", port: 8443, password: "" }, previous);
  assert.equal(config.password, "existing");
});

test("never exposes the password to popup state", () => {
  const result = toPublicConfig({ ...DEFAULT_CONFIG, password: "do-not-leak" });
  assert.equal(result.hasPassword, true);
  assert.equal("password" in result, false);
});

test("rejects URLs and invalid ports as proxy hosts", () => {
  assert.throws(() => normalizeConfig({ host: "https://example.com" }), /地址/);
  assert.throws(() => normalizeConfig({ port: 70000 }), /端口/);
});

test("requires credentials before enabling", () => {
  assert.throws(() => assertReady(DEFAULT_CONFIG), /用户名和密码/);
});
