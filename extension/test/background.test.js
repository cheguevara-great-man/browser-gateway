import test from "node:test";
import assert from "node:assert/strict";

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
    __events: { runtimeMessage, authRequired },
    __proxySetCount() { return proxySetCount; },
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
