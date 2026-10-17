#!/bin/bash
set -e

PROFILE="${AWS_PROFILE:-mcp-service}"

cd "$(dirname "$0")/../infra"

# Install dependencies only if node_modules is missing
if [ ! -d node_modules ]; then
  npm install
fi

AWS_PROFILE="$PROFILE" npx cdk deploy --require-approval never --outputs-file ../cdk-outputs.json
echo "Deployed. API endpoint:"
jq -r '.LibbyMcpStack.ApiEndpoint' ../cdk-outputs.json
