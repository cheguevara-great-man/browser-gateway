# Security

- Never commit `deployment.local.json`, `extension/runtime-config.json`, or the generated proxy password. Both files are ignored by Git.
- The proxy must never run without authentication.
- Each device should eventually receive an independent credential so it can be revoked separately.
- The extension supplies credentials only for a proxy challenge whose host and port exactly match its saved server.
- The popup never reads the stored password back from the background worker.
- `runtime-config.json` is imported into Chrome storage on first load; it exists only to avoid manually copying the generated password and is not web-accessible to normal pages.
- The server blocks private, loopback, link-local, self-referential, and non-Web destination ports.
- Certificate renewal runs twice daily because Let's Encrypt IP certificates are short-lived.
- Browser proxy credentials are protected against network interception by TLS, but `chrome.storage.local` is not a defense against malware already running as the Windows user.
- Rotate any SSH password or proxy credential that has been exposed in chat, logs, screenshots, or a support bundle.
