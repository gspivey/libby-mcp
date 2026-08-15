#!/bin/bash
set -e
OUTPUTS_FILE="${1:-cdk-outputs.json}"
SECRET_ARN=$(jq -r '.LibbyMcpStack.SecretArn' "$OUTPUTS_FILE")
aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text
