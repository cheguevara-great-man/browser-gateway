// Snapshot decoded from the locally installed FanVPN 1.6.3 configuration.
// Only public HTTPS proxy endpoints are retained; no decryption keys or credentials.
export const PRIVATE_SERVER_ID = "private";

export const FANVPN_SERVERS = Object.freeze([
  { name: "美国·01", host: "fan.365747.xyz", port: 443 },
  { name: "美国·洛杉矶（三网优化）", host: "fan.596189.xyz", port: 443 },
  { name: "美国·圣何塞（三网优化）", host: "fan.226278.xyz", port: 443 },
  { name: "日本（联通优化）", host: "fan.571589.xyz", port: 443 },
  { name: "香港（移动优化）", host: "fan.240104.xyz", port: 443 },
  { name: "德国·01（住宅·原生）", host: "fan.973511.xyz", port: 443 },
  { name: "法国·01（住宅·原生）", host: "fan.132031.xyz", port: 443 },
].map(server => Object.freeze({ ...server, id: `fanvpn:${server.host}:${server.port}` })));

export function normalizeServerId(value = PRIVATE_SERVER_ID) {
  if (value === PRIVATE_SERVER_ID || FANVPN_SERVERS.some(server => server.id === value)) return value;
  throw new Error("服务器选项无效，请重新选择服务器");
}

export function serverOptions(config) {
  return [
    { id: PRIVATE_SERVER_ID, name: "私人服务器（原有配置）", host: config.host, port: config.port, requiresCredentials: true },
    ...FANVPN_SERVERS.map(server => ({ ...server, name: `FanVPN · ${server.name}`, requiresCredentials: false })),
  ];
}

export function effectiveProxyConfig(config) {
  const serverId = normalizeServerId(config.serverId);
  if (serverId === PRIVATE_SERVER_ID) return config;
  const server = FANVPN_SERVERS.find(server => server.id === serverId);
  // Private settings remain in storage for switching back. Never reuse private
  // credentials or the private expected exit IP for a third-party proxy.
  return { ...config, host: server.host, port: server.port, username: "", password: "", expectedIp: "" };
}
