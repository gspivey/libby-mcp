#!/usr/bin/env node
import * as cdk from "aws-cdk-lib/core";
import { LibbyMcpStack } from "../lib/libby-mcp-stack";

const app = new cdk.App();
new LibbyMcpStack(app, "LibbyMcpStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
