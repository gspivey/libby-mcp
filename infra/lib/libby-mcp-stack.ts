import * as cdk from "aws-cdk-lib/core";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as logs from "aws-cdk-lib/aws-logs";
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha";
import { Construct } from "constructs";

export class LibbyMcpStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // OAuth is opt-in via CDK context (`-c oauthEnabled=true`), defaulting to
    // disabled. When disabled, this stack is byte-for-byte identical to v2
    // (AC-E4.4 / NFR-6.2): no OAuth routes, no signing-key secret, no
    // LIBBY_MCP_PUBLIC_BASE_URL — a no-OAuth deploy sees no change.
    const oauthEnabled = this.node.tryGetContext("oauthEnabled") === "true";

    // Secrets Manager: auto-generated API key for MCP authentication
    const apiKeySecret = new secretsmanager.Secret(this, "LibbyMcpApiKey", {
      secretName: "LibbyMcpApiKey",
      description: "API key for authenticating MCP clients to the Libby MCP server",
      generateSecretString: {
        excludePunctuation: true,
        passwordLength: 48,
      },
    });

    // CloudWatch Log Group with 14-day retention
    const logGroup = new logs.LogGroup(this, "LibbyMcpLogGroup", {
      logGroupName: "/aws/lambda/libby-mcp",
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Lambda function using PythonFunction for automatic dependency bundling.
    // Runtime stays Python 3.12 (HIGH-11 / NFR-6.4) — never downgraded, since a
    // downgrade would be a breaking change contradicting AC-E4.1 / NFR-6.3.
    const handler = new PythonFunction(this, "LibbyMcpHandler", {
      entry: "..",
      index: "src/handler.py",
      handler: "lambda_handler",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      memorySize: 256,
      timeout: cdk.Duration.seconds(30),
      environment: {
        LIBBY_MCP_API_KEY: apiKeySecret.secretValue.unsafeUnwrap(),
      },
      logGroup,
      bundling: {
        // Exclude large directories from the rsync inside the Docker container.
        // .dockerignore only affects the Docker image build context, not the
        // rsync that copies /asset-input -> /asset-output inside the container.
        assetExcludes: [
          ".git",
          "infra",
          ".venv",
          "__pycache__",
          "*.pyc",
          ".pytest_cache",
          ".mypy_cache",
          ".ruff_cache",
          "cdk.out",
          "node_modules",
        ],
      },
    });

    // API Gateway HTTP API
    const httpApi = new apigatewayv2.HttpApi(this, "LibbyMcpApi", {
      apiName: "libby-mcp",
      description: "MCP server for OverDrive/Libby library catalog search",
    });

    // Configure throttling on the default stage
    const stage = httpApi.defaultStage?.node.defaultChild as apigatewayv2.CfnStage;
    stage.defaultRouteSettings = {
      throttlingBurstLimit: 10,
      throttlingRateLimit: 5,
    };

    // POST /mcp route
    const lambdaIntegration = new integrations.HttpLambdaIntegration(
      "LambdaIntegration",
      handler
    );

    httpApi.addRoutes({
      path: "/mcp",
      methods: [apigatewayv2.HttpMethod.POST],
      integration: lambdaIntegration,
    });

    // GET /mcp route — routes GET requests to the Lambda instead of returning a
    // 404 from API Gateway. The AuthMiddleware rejects all non-POST methods with
    // 405 Method Not Allowed before the auth check runs, so both authenticated
    // and unauthenticated GET requests receive a spec-compliant 405 (FR-4.3).
    httpApi.addRoutes({
      path: "/mcp",
      methods: [apigatewayv2.HttpMethod.GET],
      integration: lambdaIntegration,
    });

    // --- OAuth routes + env wiring (Group F), added ONLY when OAuth is enabled.
    // All six routes bind to the SAME Lambda integration as /mcp; the ASGI
    // router dispatches internally by path, so no new Lambda/integration is
    // created (AC-E4.5 / design F2). When disabled, none of this runs and the
    // stack is identical to v2 (AC-E4.4).
    if (oauthEnabled) {
      // OAuth endpoint set (AC-E4.5). GET and POST /authorize are both routed so
      // the consent page renders (GET) and the owner-secret form submits (POST).
      const oauthRoutes: { path: string; method: apigatewayv2.HttpMethod }[] = [
        { path: "/.well-known/oauth-protected-resource", method: apigatewayv2.HttpMethod.GET },
        { path: "/.well-known/oauth-authorization-server", method: apigatewayv2.HttpMethod.GET },
        { path: "/register", method: apigatewayv2.HttpMethod.POST },
        { path: "/authorize", method: apigatewayv2.HttpMethod.GET },
        { path: "/authorize", method: apigatewayv2.HttpMethod.POST },
        { path: "/token", method: apigatewayv2.HttpMethod.POST },
      ];
      for (const r of oauthRoutes) {
        httpApi.addRoutes({ path: r.path, methods: [r.method], integration: lambdaIntegration });
      }

      // Stateless embedded AS signing key (CRITICAL-2 / HIGH-9): a clean 64-char
      // alphanumeric string (64 bytes >= 32), so the SHA-256-of-raw-bytes HMAC
      // key is unambiguous. Explicit, non-punctuation settings avoid a key that
      // could fail a length/decode check.
      const signingKeySecret = new secretsmanager.Secret(this, "LibbyMcpOAuthSigningKey", {
        secretName: "LibbyMcpOAuthSigningKey",
        description: "HMAC signing key for the embedded OAuth authorization server",
        generateSecretString: {
          passwordLength: 64,
          excludePunctuation: true,
          requireEachIncludedType: false,
        },
      });

      // Trusted public base URL (HIGH-10 / AC-E4.7): the API origin (NOT the
      // /mcp path). Defaults to httpApi.apiEndpoint, overridable with a custom
      // domain via `-c publicBaseUrl=https://mcp.example.com`. Without this the
      // OAuth routes would hit the non-loopback oauth_not_configured guard.
      const publicBaseUrl =
        (this.node.tryGetContext("publicBaseUrl") as string | undefined) ?? httpApi.apiEndpoint;

      handler.addEnvironment("LIBBY_MCP_OAUTH_ENABLED", "true");
      handler.addEnvironment("LIBBY_MCP_OAUTH_SIGNING_KEY", signingKeySecret.secretValue.unsafeUnwrap());
      handler.addEnvironment("LIBBY_MCP_PUBLIC_BASE_URL", publicBaseUrl);
    }

    // Stack outputs
    new cdk.CfnOutput(this, "ApiEndpoint", {
      value: `${httpApi.apiEndpoint}/mcp`,
      description: "MCP server endpoint URL",
    });

    new cdk.CfnOutput(this, "SecretArn", {
      value: apiKeySecret.secretArn,
      description: "ARN of the Secrets Manager secret containing the MCP API key",
    });
  }
}
