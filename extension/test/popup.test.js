import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { normalizeConfig, toPublicConfig } from "../src/config.js";
import { FANVPN_SERVERS, serverOptions } from "../src/server-catalog.js";

async function popupFixture() {
  const html = await readFile(new URL("../popup.html", import.meta.url), "utf8");
  const source = await readFile(new URL("../popup.js", import.meta.url), "utf8");
  const element = () => ({
    value: "", children: [], label: { hidden: false }, listeners: {},
    classList: { toggle() {} },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    replaceChildren(...children) { this.children = children; },
    closest() { return this.label; },
  });
  const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], element()]));
  let config = normalizeConfig({
    host: "private.example", port: 8443, username: "alice", password: "private-secret", expectedIp: "192.0.2.8",
  });
  const messages = [];
  runInNewContext(source, {
    document: { getElementById: id => elements[id], createElement: element },
    chrome: { runtime: { async sendMessage(payload) {
      messages.push(JSON.parse(JSON.stringify(payload)));
      if (payload.type === "SAVE_CONFIG") config = normalizeConfig(payload.config, config);
      if (payload.type === "SET_ENABLED") config = normalizeConfig({ enabled: payload.enabled }, config);
      return { ok: true, config: toPublicConfig(config), servers: serverOptions(config), levelOfControl: "controllable_by_this_extension" };
    } } },
  });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  return { elements, messages, flush, config: () => config };
}

test("popup lists private and FanVPN servers without applying a proxy when the selection changes", async () => {
  const { elements, messages } = await popupFixture();
  assert.equal(elements.serverId.children.length, 8);
  assert.equal(elements.serverId.value, "private");
  elements.serverId.value = FANVPN_SERVERS[0].id;
  elements.serverId.listeners.change();
  assert.equal(elements.host.value, FANVPN_SERVERS[0].host);
  assert.equal(elements.host.disabled, true);
  assert.equal(elements.username.required, false);
  assert.equal(elements.username.label.hidden, true);
  assert.equal(elements.password.label.hidden, true);
  assert.deepEqual(messages.map(message => message.type), ["GET_STATE"]);
});

test("saving a FanVPN selection excludes credentials and switching back restores private fields", async () => {
  const fixture = await popupFixture();
  const { elements, messages, flush } = fixture;
  elements.serverId.value = FANVPN_SERVERS[3].id;
  elements.serverId.listeners.change();
  elements.settingsForm.listeners.submit({ preventDefault() {} });
  await flush();
  assert.deepEqual(messages.at(-1), {
    type: "SAVE_CONFIG", config: { serverId: FANVPN_SERVERS[3].id, routingMode: "rule" },
  });
  assert.equal(fixture.config().password, "private-secret");
  elements.serverId.value = "private";
  elements.serverId.listeners.change();
  assert.equal(elements.host.value, "private.example");
  assert.equal(elements.port.value, 8443);
  assert.equal(elements.username.value, "alice");
  assert.equal(elements.username.label.hidden, false);
  assert.equal(elements.password.label.hidden, false);
  assert.equal(elements.host.disabled, false);
});
