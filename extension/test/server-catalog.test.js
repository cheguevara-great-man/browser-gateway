import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_CONFIG, assertReady, normalizeConfig } from "../src/config.js";
import { FANVPN_SERVERS, effectiveProxyConfig, serverOptions } from "../src/server-catalog.js";

test("ships the seven decoded FanVPN nodes with distinct stable identities", () => {
  assert.equal(FANVPN_SERVERS.length, 7);
  assert.equal(new Set(FANVPN_SERVERS.map(server => server.id)).size, 7);
  for (const server of FANVPN_SERVERS) {
    assert.equal(server.port, 443);
    assert.match(server.host, /^fan\.[0-9]+\.xyz$/);
    assert.equal(Object.hasOwn(server, "password"), false);
  }
});

test("old configurations keep the private server selected without changing credentials", () => {
  const config = normalizeConfig({ username: "alice", password: "private-secret" });
  assert.equal(config.serverId, "private");
  assert.equal(effectiveProxyConfig(config), config);
  assertReady(config);
});

test("FanVPN uses its own endpoint without leaking private credentials or exit assumptions", () => {
  const config = normalizeConfig({
    serverId: FANVPN_SERVERS[0].id,
    username: "private-user", password: "private-secret", expectedIp: "192.0.2.8",
  });
  assertReady(config);
  const effective = effectiveProxyConfig(config);
  assert.equal(effective.host, FANVPN_SERVERS[0].host);
  assert.equal(effective.username, "");
  assert.equal(effective.password, "");
  assert.equal(effective.expectedIp, "");
  assert.equal(config.username, "private-user");
  assert.equal(config.password, "private-secret");
});

test("FanVPN does not require private enrollment and arbitrary server IDs fail closed", () => {
  assertReady(normalizeConfig({ serverId: FANVPN_SERVERS[0].id }));
  assert.throws(() => normalizeConfig({ serverId: "fanvpn:unknown:443" }), /服务器选项无效/);
  assert.throws(() => effectiveProxyConfig({ ...DEFAULT_CONFIG, serverId: "unknown" }), /服务器选项无效/);
  assert.equal(serverOptions(DEFAULT_CONFIG).length, 8);
});
