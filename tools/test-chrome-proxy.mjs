import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const credentialPath = process.argv[2];
if (!credentialPath) {
  throw new Error("usage: node tools/test-chrome-proxy.mjs <credential-json>");
}

const credentials = JSON.parse(await fs.readFile(path.resolve(credentialPath), "utf8"));
const repositoryRoot = path.resolve(import.meta.dirname, "..");
const extensionPath = path.join(repositoryRoot, "extension");
const manifest = JSON.parse(await fs.readFile(path.join(extensionPath, "manifest.json"), "utf8"));
const extensionId = [...crypto.createHash("sha256").update(Buffer.from(manifest.key, "base64")).digest().subarray(0, 16)]
  .flatMap((byte) => [byte >> 4, byte & 0x0f])
  .map((nibble) => String.fromCharCode("a".charCodeAt(0) + nibble))
  .join("");
const userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), "browser-gateway-chromium-"));
const netLogPath = path.join(userDataDir, "netlog.json");
const launchOptions = {
  channel: "chromium",
  headless: true,
  args: [
    `--disable-extensions-except=${extensionPath}`,
    `--load-extension=${extensionPath}`,
    `--log-net-log=${netLogPath}`,
    "--net-log-capture-mode=Default",
  ],
};

let context;
try {
  context = await chromium.launchPersistentContext(userDataDir, launchOptions);

  // manifest.json contains a stable public key, so unpacked installs use a
  // deterministic ID. Opening the popup also wakes the MV3 service worker.
  const control = await context.newPage();
  await control.goto(`chrome-extension://${extensionId}/popup.html`);
  let [worker] = context.serviceWorkers();
  if (!worker) worker = await context.waitForEvent("serviceworker", { timeout: 15_000 });

  const configured = await control.evaluate(async (config) => {
    const send = (message) => chrome.runtime.sendMessage(message);
    const saved = await send({ type: "SAVE_CONFIG", config: { ...config, enabled: false } });
    if (!saved?.ok) return saved;
    return send({ type: "SET_ENABLED", enabled: true });
  }, credentials);
  assert.equal(configured?.ok, true, configured?.error ?? "extension rejected the gateway configuration");

  const page = await context.newPage();
  const started = performance.now();
  await page.goto("https://api.ipify.org?format=json", { waitUntil: "domcontentloaded", timeout: 30_000 });
  const egress = JSON.parse(await page.textContent("body"));
  assert.equal(egress.ip, credentials.expectedIp, "Chrome used an unexpected egress IP");
  const firstRequestMs = Math.round(performance.now() - started);

  // Warm the target connection once, then use a median so one slow public
  // endpoint response does not make the proxy reuse result misleading.
  const warmUrl = "https://www.gstatic.com/generate_204";
  const navigate204 = async (url) => {
    try {
      await page.goto(url, { waitUntil: "commit", timeout: 30_000 });
    } catch (error) {
      // Chromium reports an aborted navigation for an intentional 204 page.
      if (!String(error).includes("ERR_ABORTED")) throw error;
    }
  };
  await navigate204(`${warmUrl}?warmup=1`);
  const warmSamplesMs = [];
  for (let index = 0; index < 5; index += 1) {
    const warmStarted = performance.now();
    await navigate204(`${warmUrl}?sample=${index}`);
    warmSamplesMs.push(Math.round(performance.now() - warmStarted));
  }
  const orderedWarmSamples = [...warmSamplesMs].sort((left, right) => left - right);
  const warmRequestMs = orderedWarmSamples[Math.floor(orderedWarmSamples.length / 2)];

  const chatgptPage = await context.newPage();
  const checks = await Promise.all([
    page.goto("https://github.com/", { waitUntil: "domcontentloaded", timeout: 30_000 }),
    chatgptPage.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: 30_000 }),
  ]);
  assert.ok(checks[0]?.status() < 500, `GitHub returned ${checks[0]?.status()}`);
  assert.ok(checks[1]?.status() < 500, `ChatGPT returned ${checks[1]?.status()}`);

  await context.close();
  context = null;

  // Relaunch the same profile without opening the extension UI or supplying
  // credentials again. This catches the real cold-start race that otherwise
  // presents Chrome's native proxy username/password dialog.
  const restartStarted = performance.now();
  context = await chromium.launchPersistentContext(userDataDir, launchOptions);
  const restartPage = await context.newPage();
  await restartPage.goto("https://api.ipify.org?format=json&after-restart=1", {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  const restartEgress = JSON.parse(await restartPage.textContent("body"));
  assert.equal(restartEgress.ip, credentials.expectedIp, "cold restart lost proxy authentication");
  const restartRequestMs = Math.round(performance.now() - restartStarted);
  await context.close();
  context = null;

  const netLog = JSON.parse(await fs.readFile(netLogPath, "utf8"));
  const typeNames = new Map(
    Object.entries(netLog.constants?.logEventTypes ?? {}).map(([name, id]) => [id, name]),
  );
  const h2Events = netLog.events.filter((event) => typeNames.get(event.type)?.includes("HTTP2_SESSION"));
  const proxyH2Events = h2Events.filter((event) => JSON.stringify(event.params ?? {}).includes(String(credentials.host)));
  assert.ok(proxyH2Events.length > 0, "Chrome did not negotiate an HTTP/2 proxy session");

  console.log(JSON.stringify({
    ok: true,
    extensionId,
    egressIp: egress.ip,
    firstRequestMs,
    warmRequestMs,
    warmSamplesMs,
    restartRequestMs,
    http2Events: h2Events.length,
    proxyHttp2Events: proxyH2Events.length,
  }));
} finally {
  if (context) await context.close().catch(() => {});
  await fs.rm(userDataDir, { recursive: true, force: true });
}
