/**
 * Auth toggle tests (FR-ID-2/3): dev persona resolution and oidc JWT
 * verification against an inline JWKS, fully hermetic - keys are generated
 * per run and no network or IdP is involved.
 */
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { AuthError, authConfig, resolveClaims } from "./auth.js";

const ISSUER = "https://idp.test/realms/cloudops";
const AUDIENCE = "cloudops-console";

let privateKey: CryptoKey;

async function signToken(
  claims: Record<string, unknown>,
  opts: { issuer?: string; audience?: string; expired?: boolean } = {},
): Promise<string> {
  let jwt = new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: "test-key" })
    .setIssuer(opts.issuer ?? ISSUER)
    .setAudience(opts.audience ?? AUDIENCE)
    .setIssuedAt();
  jwt = opts.expired ? jwt.setExpirationTime("-5m") : jwt.setExpirationTime("5m");
  return jwt.sign(privateKey);
}

beforeAll(async () => {
  const pair = await generateKeyPair("RS256");
  privateKey = pair.privateKey as CryptoKey;
  const jwk = await exportJWK(pair.publicKey);
  process.env.CLOUDOPS_OIDC_JWKS = JSON.stringify({ keys: [{ ...jwk, kid: "test-key", alg: "RS256" }] });
  process.env.CLOUDOPS_OIDC_ISSUER = ISSUER;
  process.env.CLOUDOPS_OIDC_AUDIENCE = AUDIENCE;
});

afterEach(() => {
  delete process.env.CLOUDOPS_AUTH_MODE;
});

describe("dev mode", () => {
  it("defaults to dev and resolves empty claims for an unknown persona", async () => {
    expect(authConfig().mode).toBe("dev");
    const claims = await resolveClaims({ userSub: "nobody-known" });
    expect(claims.sub).toBe("");
    expect(claims.groups).toEqual([]);
  });

  it("ignores any bearer token in dev mode", async () => {
    const claims = await resolveClaims({ authorization: "Bearer garbage", userSub: "nobody" });
    expect(claims.sub).toBe("");
  });
});

describe("oidc mode", () => {
  beforeAll(() => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
  });

  it("accepts a valid token and maps the claim shape", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({
      sub: "u-123",
      name: "Payments Dev",
      email: "dev@example.test",
      groups: ["payments-eng", "retail"],
    });
    const claims = await resolveClaims({ authorization: `Bearer ${token}` });
    expect(claims).toEqual({
      sub: "u-123",
      name: "Payments Dev",
      email: "dev@example.test",
      groups: ["payments-eng", "retail"],
    });
  });

  it("accepts a space-separated string groups claim", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ sub: "u-1", groups: "payments-eng retail-sre" });
    const claims = await resolveClaims({ authorization: `Bearer ${token}` });
    expect(claims.groups).toEqual(["payments-eng", "retail-sre"]);
  });

  it("reads a custom groups claim name", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    process.env.CLOUDOPS_OIDC_GROUPS_CLAIM = "cognito:groups";
    const token = await signToken({ sub: "u-1", "cognito:groups": ["a", "b"] });
    const claims = await resolveClaims({ authorization: `Bearer ${token}` });
    delete process.env.CLOUDOPS_OIDC_GROUPS_CLAIM;
    expect(claims.groups).toEqual(["a", "b"]);
  });

  it("rejects a missing token with a 401 AuthError", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    await expect(resolveClaims({ userSub: "picker-is-ignored" })).rejects.toBeInstanceOf(AuthError);
  });

  it("rejects an expired token", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ sub: "u-1" }, { expired: true });
    await expect(resolveClaims({ authorization: `Bearer ${token}` })).rejects.toBeInstanceOf(AuthError);
  });

  it("rejects a wrong issuer", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ sub: "u-1" }, { issuer: "https://evil.test" });
    await expect(resolveClaims({ authorization: `Bearer ${token}` })).rejects.toBeInstanceOf(AuthError);
  });

  it("rejects a wrong audience", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ sub: "u-1" }, { audience: "another-app" });
    await expect(resolveClaims({ authorization: `Bearer ${token}` })).rejects.toBeInstanceOf(AuthError);
  });

  it("rejects a token with no sub", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ name: "No Subject" });
    await expect(resolveClaims({ authorization: `Bearer ${token}` })).rejects.toBeInstanceOf(AuthError);
  });

  it("keeps the public 401 message generic", async () => {
    process.env.CLOUDOPS_AUTH_MODE = "oidc";
    const token = await signToken({ sub: "u-1" }, { issuer: "https://evil.test" });
    const err = await resolveClaims({ authorization: `Bearer ${token}` }).catch((e: AuthError) => e);
    expect((err as AuthError).publicMessage).not.toMatch(/issuer|evil|jwt|signature/i);
  });
});
