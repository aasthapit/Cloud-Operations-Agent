/**
 * Structured logging for the web tier (NFR-OBS-3, NFR-LOG-1).
 * pino with (a) key-based redaction for known secret-bearing paths and
 * (b) a value scrubber mirroring the Python redaction layer's patterns,
 * so the same canary test holds across both tiers.
 */
import pino from "pino";

import { config } from "./config.js";

const MASK = "[REDACTED]";

/** Values of secret-shaped env vars must never appear in any log line. */
const envSecretValues = Object.entries(process.env)
  .filter(([k, v]) => /(TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|CREDENTIAL|PRIVATE)/i.test(k) && v && v.length >= 6)
  .map(([, v]) => v as string)
  .sort((a, b) => b.length - a.length);

const patterns: Array<[RegExp, string]> = [
  [/\bBearer\s+[A-Za-z0-9\-._~+/=]{8,}/gi, `Bearer ${MASK}`],
  [/-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----/g, `-----PEM ${MASK}-----`],
  [/\b((?:api[_-]?key|token|secret|password|passwd|client[_-]?secret)\s*[:=]\s*)["']?[^\s"',;]{4,}["']?/gi, `$1${MASK}`],
];

export function scrub(text: string): string {
  let out = text;
  for (const v of envSecretValues) out = out.split(v).join(MASK);
  for (const [re, repl] of patterns) out = out.replace(re, repl);
  return out;
}

function scrubDeep(value: unknown, depth = 0): unknown {
  if (depth > 10) return MASK;
  if (typeof value === "string") return scrub(value);
  if (Array.isArray(value)) return value.map((v) => scrubDeep(v, depth + 1));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = /(authorization|cookie|token|secret|password|passwd|api[_-]?key|credential)/i.test(k)
        ? MASK
        : scrubDeep(v, depth + 1);
    }
    return out;
  }
  return value;
}

export const logger = pino({
  level: config.logLevel,
  // Belt: pino path redaction for the obvious carriers.
  redact: { paths: ["req.headers.authorization", "req.headers.cookie"], censor: MASK },
  // Braces: scrub every serialized value (last hook before output).
  hooks: {
    logMethod(args, method) {
      const scrubbed = args.map((a) =>
        typeof a === "string" ? scrub(a) : scrubDeep(a),
      ) as Parameters<typeof method>;
      method.apply(this, scrubbed);
    },
  },
  transport: config.isDev() ? { target: "pino-pretty", options: { colorize: true } } : undefined,
});
