#!/bin/bash
set -e
cd "$(dirname "$0")/../infra"
npx cdk destroy --force
