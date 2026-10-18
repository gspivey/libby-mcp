// cdk synth assertions for Group E Task E4 (AC-E4.5 / AC-E4.7).
//
// Verifies that:
//   - with OAuth disabled (default), the stack is identical to v2: only the two
//     /mcp routes, no OAuth routes, no LIBBY_MCP_PUBLIC_BASE_URL on the Lambda,
//     no signing-key secret;
//   - with OAuth enabled (-c oauthEnabled=true), the six OAuth routes are added
//     (all bound to the same Lambda integration), LIBBY_MCP_PUBLIC_BASE_URL is
//     set (defaulting to the API endpoint, overridable via -c publicBaseUrl),
//     and a signing-key secret is provisioned;
//   - the Lambda runtime stays python3.12 in both cases (HIGH-11).
//
// Run with:  npm test   (node --test via tsx)

import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as cdk from "aws-cdk-lib/core";
import { Template, Match } from "aws-cdk-lib/assertions";
import { LibbyMcpStack } from "../lib/libby-mcp-stack";

function synth(context: Record<string, unknown> = {}): Template {
  const app = new cdk.App({
    context: {
      // Skip Docker-based asset bundling during synth so these assertions run
      // without a Docker daemon (CI does not stand up Docker for infra tests).
      // An empty bundling-stacks list disables PythonFunction bundling.
      "aws:cdk:bundling-stacks": [],
      ...context,
    },
  });
  const stack = new LibbyMcpStack(app, "TestStack");
  return Template.fromStack(stack);
}

// The OAuth paths that must be routed when OAuth is enabled (AC-E4.5).
const OAUTH_ROUTE_KEYS = [
  "GET /.well-known/oauth-protected-resource",
  "GET /.well-known/oauth-authorization-server",
  "POST /register",
  "GET /authorize",
  "POST /authorize",
  "POST /token",
];

function routeKeys(template: Template): string[] {
  const routes = template.findResources("AWS::ApiGatewayV2::Route");
  return Object.values(routes).map((r: any) => r.Properties.RouteKey as string);
}

function lambdaEnv(template: Template): Record<string, unknown> {
  const fns = template.findResources("AWS::Lambda::Function");
  // The MCP handler is the only function with the LIBBY_MCP_API_KEY env var.
  for (const fn of Object.values(fns) as any[]) {
    const vars = fn.Properties?.Environment?.Variables ?? {};
    if ("LIBBY_MCP_API_KEY" in vars) return vars;
  }
  throw new Error("MCP handler Lambda not found");
}

test("OAuth disabled (default): stack is identical to v2 — only /mcp routes", () => {
  const template = synth();
  const keys = routeKeys(template).sort();
  assert.deepEqual(keys, ["GET /mcp", "POST /mcp"].sort());

  // No OAuth route is present.
  for (const k of OAUTH_ROUTE_KEYS) {
    assert.ok(!keys.includes(k), `unexpected OAuth route when disabled: ${k}`);
  }

  // No LIBBY_MCP_PUBLIC_BASE_URL / OAuth env vars on the Lambda.
  const env = lambdaEnv(template);
  assert.ok(!("LIBBY_MCP_PUBLIC_BASE_URL" in env), "base URL set while OAuth disabled");
  assert.ok(!("LIBBY_MCP_OAUTH_ENABLED" in env), "OAuth-enabled flag set while disabled");
  assert.ok(!("LIBBY_MCP_OAUTH_SIGNING_KEY" in env), "signing key set while disabled");

  // No signing-key secret: only the single API-key secret exists.
  template.resourceCountIs("AWS::SecretsManager::Secret", 1);

  // Runtime stays python3.12 (HIGH-11).
  template.hasResourceProperties("AWS::Lambda::Function", {
    Runtime: "python3.12",
  });
});

test("OAuth enabled: six OAuth routes added, all bound to a Lambda integration", () => {
  const template = synth({ oauthEnabled: "true" });
  const keys = routeKeys(template);
  for (const k of OAUTH_ROUTE_KEYS) {
    assert.ok(keys.includes(k), `missing OAuth route when enabled: ${k}`);
  }
  // The two /mcp routes remain.
  assert.ok(keys.includes("GET /mcp") && keys.includes("POST /mcp"));

  // Every route targets an integration (the same Lambda integration).
  const routes = template.findResources("AWS::ApiGatewayV2::Route");
  for (const r of Object.values(routes) as any[]) {
    assert.ok(r.Properties.Target, `route ${r.Properties.RouteKey} has no integration target`);
  }
  // Exactly one integration is created (all routes share it).
  template.resourceCountIs("AWS::ApiGatewayV2::Integration", 1);
});

test("OAuth enabled: LIBBY_MCP_PUBLIC_BASE_URL set, signing-key secret provisioned", () => {
  const template = synth({ oauthEnabled: "true" });
  const env = lambdaEnv(template);
  assert.equal(env.LIBBY_MCP_OAUTH_ENABLED, "true");
  assert.ok("LIBBY_MCP_PUBLIC_BASE_URL" in env, "base URL must be set when OAuth enabled");
  assert.ok("LIBBY_MCP_OAUTH_SIGNING_KEY" in env, "signing key must be injected when OAuth enabled");

  // A second (signing-key) secret is provisioned with explicit length settings.
  template.resourceCountIs("AWS::SecretsManager::Secret", 2);
  template.hasResourceProperties("AWS::SecretsManager::Secret", {
    GenerateSecretString: Match.objectLike({
      PasswordLength: 64,
      ExcludePunctuation: true,
    }),
  });

  // Runtime still python3.12.
  template.hasResourceProperties("AWS::Lambda::Function", { Runtime: "python3.12" });
});

test("OAuth enabled: -c publicBaseUrl override is honored", () => {
  const template = synth({ oauthEnabled: "true", publicBaseUrl: "https://mcp.example.com" });
  const env = lambdaEnv(template);
  assert.equal(env.LIBBY_MCP_PUBLIC_BASE_URL, "https://mcp.example.com");
});
