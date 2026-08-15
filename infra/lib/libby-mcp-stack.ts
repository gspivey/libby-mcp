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

    // Lambda function using PythonFunction for automatic dependency bundling
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
