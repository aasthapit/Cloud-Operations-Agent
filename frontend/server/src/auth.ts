/**
 * Identity resolution with a config toggle (PRD D5, FR-ID-1..4).
 *
 * CLOUDOPS_AUTH_MODE=dev (default): the browser's picker selects a persona
 * from config/identity/users.yaml and full claims resolve here; an unknown
 * or absent selection yields empty claims, which the agent answers with
 * onboarding guidance rather than an error (FR-ID-4).
 *
 * CLOUDOPS_AUTH_MODE=oidc: every chat request must carry
 * `Authorization: Bearer <JWT>`; the token is verified against the
 * configured JWKS, issuer, and audience, then mapped to the same claim
 * shape. The client-supplied userSub is ignored on purpose: in this mode
 * the token IS the identity.
 *
 * The claim shape never differs between modes, so nothing downstream (BFF
 * relay, A2A metadata, agent context resolution) can tell which mode
 * produced it. That symmetry is the whole point of FR-ID-3's seam.
 */
import fs from "node:fs";
import path from "node:path";

import { createLocalJWKSet, createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";
import YAML from "yaml";

import type { A2AClaims } from "./a2a.js";
import { config } from "./config.js";
import { logger } from "./logger.js";

/** 401-with-a-public-sentence; never leaks verification internals. */
export class AuthError extends Error {
  readonly status = 401;
  constructor(public readonly publicMessage: string, detail?: string) {
    super(detail ?? publicMessage);
  }
}

export type AuthMode = "dev" | "oidc";

/** Read fresh per call so a restart is the only requirement for a mode
 * change and tests can flip env between cases. */
export function authConfig(): {
  mode: AuthMode;
  issuer: string;
  audience: string;
  jwksUrl: string;
  jwksInline: string;
  groupsClaim: string;
} {
  const rawMode = (process.env.CLOUDOPS_AUTH_MODE ?? "dev").toLowerCase();
  return {
    mode: rawMode === "oidc" ? "oidc" : "dev",
    issuer: process.env.CLOUDOPS_OIDC_ISSUER ?? "",
    audience: process.env.CLOUDOPS_OIDC_AUDIENCE ?? "",
    jwksUrl: process.env.CLOUDOPS_OIDC_JWKS_URL ?? "",
    // Inline JWKS JSON: air-gapped setups and hermetic tests; wins over URL.
    jwksInline: process.env.CLOUDOPS_OIDC_JWKS ?? "",
    groupsClaim: process.env.CLOUDOPS_OIDC_GROUPS_CLAIM ?? "groups",
  };
}

/** Dev personas, read fresh per request so users.yaml hot-reloads (F9). */
export function loadUsers(): A2AClaims[] {
  const file = path.join(config.configDir, "identity", "users.yaml");
  const doc = YAML.parse(fs.readFileSync(file, "utf8")) as { users?: A2AClaims[] };
  return doc.users ?? [];
}

/** JWKS key source, cached per configuration so remote JWKS fetches are
 * reused across requests but a config change invalidates cleanly. */
let cachedKey: { fingerprint: string; source: JWTVerifyGetKey } | null = null;

function keySource(): JWTVerifyGetKey {
  const { jwksInline, jwksUrl } = authConfig();
  const fingerprint = jwksInline ? `inline:${jwksInline}` : `url:${jwksUrl}`;
  if (cachedKey?.fingerprint === fingerprint) return cachedKey.source;
  let source: JWTVerifyGetKey;
  if (jwksInline) {
    source = createLocalJWKSet(JSON.parse(jwksInline));
  } else if (jwksUrl) {
    source = createRemoteJWKSet(new URL(jwksUrl));
  } else {
    throw new AuthError(
      "authentication is misconfigured on the server",
      "oidc mode requires CLOUDOPS_OIDC_JWKS_URL or CLOUDOPS_OIDC_JWKS",
    );
  }
  cachedKey = { fingerprint, source };
  return source;
}

function asGroups(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  // Some IdPs emit a single space- or comma-separated string claim.
  if (typeof value === "string" && value.trim()) return value.split(/[\s,]+/).filter(Boolean);
  return [];
}

/**
 * Resolve the request's identity claims per the active mode.
 * Throws AuthError only in oidc mode; dev mode degrades to empty claims.
 */
export async function resolveClaims(input: {
  authorization?: string;
  userSub?: string;
}): Promise<A2AClaims> {
  const auth = authConfig();
  if (auth.mode === "dev") {
    try {
      const found = loadUsers().find((u) => u.sub === input.userSub);
      if (found) return found;
    } catch (err) {
      logger.warn({ err }, "auth.users_load_failed");
    }
    return { sub: "", name: "", email: "", groups: [] };
  }

  const header = input.authorization ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token) {
    throw new AuthError("a bearer token is required", "missing Authorization header");
  }
  try {
    const { payload } = await jwtVerify(token, keySource(), {
      issuer: auth.issuer || undefined,
      audience: auth.audience || undefined,
    });
    if (!payload.sub) throw new Error("token has no sub claim");
    return {
      sub: String(payload.sub),
      name: String(payload.name ?? payload.preferred_username ?? ""),
      email: String(payload.email ?? ""),
      groups: asGroups(payload[auth.groupsClaim]),
    };
  } catch (err) {
    if (err instanceof AuthError) throw err;
    // Reason to the log, generic sentence to the client (NFR-LOG-2).
    logger.warn({ err: String(err) }, "auth.token_rejected");
    throw new AuthError("the bearer token was rejected");
  }
}
