import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";

const manifest = JSON.parse(await readFile(new URL("../extension/manifest.json", import.meta.url), "utf8"));
const requiredPermissions = ["proxy", "storage", "webRequest", "webRequestAuthProvider"];
for (const permission of requiredPermissions) {
  if (!manifest.permissions?.includes(permission)) throw new Error(`Missing permission: ${permission}`);
}
if (manifest.manifest_version !== 3) throw new Error("Manifest V3 is required");
if (!manifest.key) throw new Error("A stable extension identity key is required");

const digest = new Uint8Array(await webcrypto.subtle.digest("SHA-256", Buffer.from(manifest.key, "base64")));
const alphabet = "abcdefghijklmnop";
const extensionId = [...digest.slice(0, 16)]
  .flatMap((byte) => [alphabet[byte >> 4], alphabet[byte & 15]])
  .join("");

console.log(`extension manifest: OK (${extensionId})`);

\n