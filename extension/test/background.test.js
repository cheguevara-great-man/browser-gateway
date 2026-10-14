import test from "node:test";
import assert from "node:assert/strict";
import { FANVPN_SERVERS } from "../src/server-catalog.js";

class FakeEvent {
  listeners = [];
  registrations = [];
  addListener(listener, filter, extraInfoSpec) {
    this.listeners.push(listener);
    this.registrations.push({ listener, filter, extraInfoSpec });
  }
}

function createChrome(initialConfig = null) {
  const storage = initialConfig ? { gatewayConfig: structuredClone(initialConfig) } : {};
  let proxyState = { levelOfControl: "controllable_by_this_extension", value: { mode: "system" } };
  let proxySetCount = 0;
  const runtimeMessage = new FakeEvent();
  const authRequired = new FakeEvent();
  const externalMessages = [];
  const openedTabs = [];
  return {
    storage: {
      local: {
        async get(key) { return key in storage ? { [key]: storage[key] } : {}; },
        async set(values) { Object.assign(storage, values); },
      },
    },
    runtime: {
      lastError: null,
      getURL(path) { return `chrome-extension://test/${path}`; },
      onInstalled: new FakeEvent(),
      onStartup: new FakeEvent(),
      onMessage: runtimeMessage,
      async sendMessage(extensionId, message) {
        externalMessages.push({ extensionId, message });
        if (message.kind === "device-config:get") return { ok: true, state: { configured: false } };
        return { ok: true, state: { configured: true, restart_required: true } };
      },
    },
    proxy: {
      settings: {
        get(_details, callback) { callback(structuredClone(proxyState)); },
        set(details, callback) {
          proxySetCount += 1;
          proxyState = { levelOfControl: "controlled_by_this_extension", value: details.value };
          callback();
        },
        clear(_details, callback) {
          proxyState = { levelOfControl: "controllable_by_this_extension", value: { mode: "system" } };
          callback();
        },
      },
      onProxyError: new FakeEvent(),
    },
    webRequest: {
      onAuthRequired: authRequired,
      onCompleted: new FakeEvent(),
      onErrorOccurred: new FakeEvent(),
    },
    tabs: { async create(value) { openedTabs.push(value); } },
    __events: { runtimeMessage, authRequired },
    __externalMessages: externalMessages,
    __openedTabs: openedTabs,
    __proxySetCount() { return proxySetCount; },
    __setLevelOfControl(value) { proxyState.levelOfControl = value; },
  };
}

function send(listener, payload) {
  return new Promise((resolve) => listener(payload, {}, resolve));
}

test("background saves credentials, controls the proxy, authenticates narrowly, and tests egress", async () => {
  globalThis.chrome = createChrome();
  globalThis.fetch = async (url) => String(url).endsWith("runtime-config.json")
    ? { ok: false }
    : { ok: true, async json() { return { ip: "38.207.167.51" }; } };
  await import(`../src/background.js?test=${Date.now()}`);

  const listener = chrome.__events.runtimeMessage.listeners[0];
  const saved = await send(listener, {
    type: "SAVE_CONFIG",
    config: {
      host: "38.207.167.51", port: 443, username: "test-user", password: "test-password",
      expectedIp: "38.207.167.51",
    },
  });
  assert.equal(saved.ok, true);
  assert.equal(saved.config.hasPassword, true);
  assert.equal("password" in saved.config, false);

  const enabled = await send(listener, { type: "SET_ENABLED", enabled: true });
  assert.equal(enabled.active, true);

  const authListener = chrome.__events.authRequired.listeners[0];
  const accepted = await new Promise((resolve) => authListener({
    requestId: "one", isProxy: true, challenger: { host: "38.207.167.51", port: 443 },
  }, resolve));
  assert.deepEqual(accepted.authCredentials, { username: "test-user", password: "test-password" });

  const refused = await new Promise((resolve) => authListener({
    requestId: "two", isProxy: true, challenger: { host: "other.example", port: 443 },
  }, resolve));
  assert.deepEqual(refused, {});

  const tested = await send(listener, { type: "TEST_CONNECTION" });
  assert.equal(tested.lastTest.ip, "38.207.167.51");
  assert.equal(tested.lastTest.ok, true);

  const disabled = await send(listener, { type: "SET_ENABLED", enabled: false });
  assert.equal(disabled.config.enabled, false);
  assert.equal(disabled.active, false);
});

test("one-time enrollment configures Gateway and sends scoped usage identity to Bridge", async () => {
  globalThis.chrome = createChrome();
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith("runtime-config.json")) return { ok: false };
    if (String(url).endsWith("/v1/enrollment/redeem")) {
      assert.equal(JSON.parse(options.body).code, "ABCD-2345");
      return { ok: true, async json() { return {
        machineId: "11111111-1111-4111-8111-111111111111",
        machineName: "公司电脑-03",
        deviceToken: "device-token-secret-1234567890",
        usageCollectorUrl: "https://203.0.113.10:9443/v1/usage/events",
        dashboardUrl: "https://203.0.113.10:9443/dashboard",
        gateway: { host: "203.0.113.10", port: 443, username: "gateway-device", password: "proxy-secret", expectedIp: "203.0.113.10" },
      }; } };
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  await import(`../src/background.js?enroll=${Date.now()}`);
  const listener = chrome.__events.runtimeMessage.listeners[0];
  const result = await send(listener, {
    type: "ENROLL_DEVICE", server: "https://203.0.113.10:9443", code: "ABCD-2345",
  });
  assert.equal(result.config.enrolled, true);
  assert.equal(result.config.machineName, "公司电脑-03");
  assert.equal(result.config.hasPassword, true);
  assert.equal("deviceToken" in result.config, false);
  const applied = chrome.__externalMessages.find(({ message }) => message.kind === "device-config:apply");
  assert.equal(applied.extensionId, "bgpbajocpomglgdffkgcklhepbcfpbfd");
  assert.equal(applied.message.config.machineName, "公司电脑-03");
});

test("direct mode controls Chrome without gateway credentials or proxy authentication", async () => {
  globalThis.chrome = createChrome();
  globalThis.fetch = async (url) => String(url).endsWith("runtime-config.json")
    ? { ok: false }
    : { ok: true, async json() { return { ip: "198.51.100.8" }; } };
  await import(`../src/background.js?direct=${Date.now()}`);

  const listener = chrome.__events.runtimeMessage.listeners[0];
  const saved = await send(listener, {
    type: "SAVE_CONFIG",
    config: { host: "38.207.167.51", port: 443, routingMode: "direct" },
  });
  assert.equal(saved.config.routingMode, "direct");
  const enabled = await send(listener, { type: "SET_ENABLED", enabled: true });
  assert.equal(enabled.active, true);

  const authListener = chrome.__events.authRequired.listeners[0];
  const response = await new Promise((resolve) => authListener({
    requestId: "direct", isProxy: true, challenger: { host: "38.207.167.51", port: 443 },
  }, resolve));
  assert.deepEqual(response, {});

  const tested = await send(listener, { type: "TEST_CONNECTION" });
  assert.equal(tested.lastTest.ok, true);
  assert.equal(tested.lastTest.expectedIp, "");
});

test("cold startup restores the proxy once and serves stored credentials", async () => {
  globalThis.chrome = createChrome({
    host: "38.207.167.51",
    port: 443,
    username: "cold-user",
    password: "cold-password",
    expectedIp: "38.207.167.51",
    enabled: true,
  });
  const fetched = [];
  globalThis.fetch = async (url) => {
    fetched.push(String(url));
    return ({
    ok: true,
    async json() { return { ip: "38.207.167.51" }; },
    });
  };
  await import(`../src/background.js?cold=${Date.now()}`);

  const startup = chrome.runtime.onStartup.listeners[0];
  const first = startup();
  const second = startup();
  await Promise.all([first, second]);
  assert.equal(chrome.__proxySetCount(), 1);
  assert.deepEqual(fetched, ["https://api.ipify.org/?browser-gateway-auth-prime=1"]);

  const registration = chrome.__events.authRequired.registrations[0];
  assert.deepEqual(registration.filter, { urls: ["<all_urls>"] });
  assert.deepEqual(registration.extraInfoSpec, ["asyncBlocking"]);

  // A later worker startup must not reset an already identical proxy. Resetting
  // Chrome proxy settings also resets its authentication state and can trigger
  // a native username/password prompt.
  await startup();
  assert.equal(chrome.__proxySetCount(), 1);
  assert.equal(fetched.length, 2);

  const authListener = chrome.__events.authRequired.listeners[0];
  const accepted = await new Promise((resolve) => authListener({
    requestId: "cold-one",
    isProxy: true,
    challenger: { host: "38.207.167.51", port: 443 },
  }, resolve));
  assert.deepEqual(accepted.authCredentials, {
    username: "cold-user",
    password: "cold-password",
  });

  const websocket = await new Promise((resolve) => authListener({
    requestId: "cold-wss", url: "wss://chatgpt.com/socket",
    isProxy: true, challenger: { host: "38.207.167.51", port: 443 },
  }, resolve));
  assert.deepEqual(websocket.authCredentials, {
    username: "cold-user",
    password: "cold-password",
  });
});

test("switches FanVPN endpoints without sending private credentials and restores the private server", async () => {
  globalThis.chrome = createChrome({
    host: "private.example", port: 8443, username: "private-user", password: "private-secret",
    expectedIp: "192.0.2.8", enabled: false, routingMode: "global",
  });
  globalThis.fetch = async () => ({ ok: true, async json() { return { ip: "198.51.100.9" }; } });
  await import(`../src/background.js?fanvpn=${Date.now()}`);
  const listener = chrome.__events.runtimeMessage.listeners[0];
  const node = FANVPN_SERVERS[0];
  let selected = await send(listener, { type: "SAVE_CONFIG", config: { serverId: node.id } });
  assert.equal(selected.ok, true);
  assert.equal(selected.servers.length, 8);
  assert.equal(selected.config.host, "private.example");
  let enabled = await send(listener, { type: "SET_ENABLED", enabled: true });
  assert.equal(enabled.active, true);
  let proxy = await new Promise(resolve => chrome.proxy.settings.get({}, resolve));
  assert.deepEqual(proxy.value.rules.singleProxy, { scheme: "https", host: node.host, port: node.port });
  const auth = chrome.__events.authRequired.listeners[0];
  const response = await new Promise(resolve => auth({
    requestId: "fan", isProxy: true, challenger: { host: node.host, port: node.port },
  }, resolve));
  assert.deepEqual(response, {});
  const tested = await send(listener, { type: "TEST_CONNECTION" });
  assert.equal(tested.lastTest.ok, true);
  assert.equal(tested.lastTest.expectedIp, "");

  selected = await send(listener, { type: "SAVE_CONFIG", config: { serverId: FANVPN_SERVERS[1].id } });
  assert.equal(selected.active, true);
  assert.equal(selected.lastTest, null);
  proxy = await new Promise(resolve => chrome.proxy.settings.get({}, resolve));
  assert.equal(proxy.value.rules.singleProxy.host, FANVPN_SERVERS[1].host);

  selected = await send(listener, { type: "SAVE_CONFIG", config: { serverId: "private" } });
  assert.equal(selected.active, true);
  proxy = await new Promise(resolve => chrome.proxy.settings.get({}, resolve));
  assert.deepEqual(proxy.value.rules.singleProxy, { scheme: "https", host: "private.example", port: 8443 });
  const privateAuth = await new Promise(resolve => auth({
    requestId: "private", isProxy: true, challenger: { host: "private.example", port: 8443 },
  }, resolve));
  assert.deepEqual(privateAuth.authCredentials, { username: "private-user", password: "private-secret" });
});

test("FanVPN alone can be enabled with no private password and cold startup retains that node", async () => {
  globalThis.chrome = createChrome({ serverId: FANVPN_SERVERS[3].id, enabled: true, routingMode: "global" });
  globalThis.fetch = async () => ({ ok: true, async json() { return { ip: "198.51.100.9" }; } });
  await import(`../src/background.js?fanvpn-cold=${Date.now()}`);
  await chrome.runtime.onStartup.listeners[0]();
  const listener = chrome.__events.runtimeMessage.listeners[0];
  const state = await send(listener, { type: "GET_STATE" });
  assert.equal(state.active, true);
  assert.equal(state.config.serverId, FANVPN_SERVERS[3].id);
  assert.equal(state.config.hasPassword, false);
});

test("a refused node switch leaves the saved selection unchanged and never steals proxy control", async () => {
  globalThis.chrome = createChrome({ serverId: FANVPN_SERVERS[0].id, enabled: false, routingMode: "global" });
  globalThis.fetch = async () => ({ ok: true, async json() { return { ip: "198.51.100.9" }; } });
  await import(`../src/background.js?fanvpn-conflict=${Date.now()}`);
  const listener = chrome.__events.runtimeMessage.listeners[0];
  await send(listener, { type: "SET_ENABLED", enabled: true });
  const before = chrome.__proxySetCount();
  chrome.__setLevelOfControl("controlled_by_other_extensions");
  const refused = await send(listener, { type: "SAVE_CONFIG", config: { serverId: FANVPN_SERVERS[1].id } });
  assert.equal(refused.ok, false);
  assert.match(refused.error, /其他扩展控制/);
  assert.equal(chrome.__proxySetCount(), before);
  const state = await send(listener, { type: "GET_STATE" });
  assert.equal(state.config.serverId, FANVPN_SERVERS[0].id);
});
