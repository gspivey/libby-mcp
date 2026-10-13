#!/bin/bash
set -e
cd "$(dirname "$0")/../infra"
npm install
npx cdk deploy --require-approval never --outputs-file ../cdk-outputs.json
echo "Deployed. API endpoint:"
jq -r '.LibbyMcpStack.ApiEndpoint' ../cdk-outputs.json
