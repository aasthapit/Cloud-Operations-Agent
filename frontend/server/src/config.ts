/**
 * BFF process configuration from environment variables.
 * Same precedence contract as the Python services: env > config > default.
 */
import fs from "node:fs";
import path from "node:path";

/**
 * Locate the config plane. CLOUDOPS_CONFIG_DIR wins; otherwise walk up from
 * the process cwd looking for a directory containing identity/users.yaml,
 * so the BFF behaves the same launched from the repo root, frontend/, or
 * the npm workspace directory.
 */
function findConfigDir(): string {
  const fromEnv = process.env.CLOUDOPS_CONFIG_DIR;
  if (fromEnv) return path.resolve(fromEnv);
  let dir = process.cwd();
  for (let i = 0; i < 5; i++) {
    const candidate = path.join(dir, "config");
    if (fs.existsSync(path.join(candidate, "identity", "users.yaml"))) return candidate;
    dir = path.dirname(dir);
  }
  return path.resolve("config");
}

export const config = {
  port: Number(process.env.CLOUDOPS_BFF_PORT ?? 8080),
  env: process.env.CLOUDOPS_ENV ?? "dev",
  logLevel: process.env.CLOUDOPS_LOG_LEVEL ?? "info",
  /** The agent's A2A endpoint (JSON-RPC root). */
  agentUrl: process.env.CLOUDOPS_AGENT_A2A_URL ?? "http://127.0.0.1:8001",
  /** Hot-reloadable config plane root (read fresh per request where used). */
  configDir: findConfigDir(),
  /** express.json() body size limit; large A2A metadata payloads are small,
   * so the default matches the prior hardcoded "256kb". */
  bodyLimit: process.env.CLOUDOPS_BFF_BODY_LIMIT ?? "256kb",
  /** /api/meta's upstream fetch to the agent's /status; short because the
   * rail polls this and a hung agent must not hang the console. */
  statusTimeoutMs: Number(process.env.CLOUDOPS_BFF_STATUS_TIMEOUT_MS ?? 1500),
  isDev(): boolean {
    return this.env !== "prod";
  },
};
