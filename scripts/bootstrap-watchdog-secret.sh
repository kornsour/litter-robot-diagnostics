#!/usr/bin/env bash
set -euo pipefail

# Credentials are supplied interactively, never written to OpenTofu state.
secret_name=${1:?usage: bootstrap-watchdog-secret.sh SECRET_NAME [REGION]}
region=${2:-us-west-2}
read -r -p "Whisker username: " username
read -r -s -p "Whisker password: " password
printf '\n' >&2

secret_json=$(python3 -c 'import json, sys; print(json.dumps({"username": sys.argv[1], "password": sys.argv[2]}))' "$username" "$password")
if aws secretsmanager describe-secret --secret-id "$secret_name" --region "$region" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$secret_name" --secret-string "$secret_json" --region "$region" >/dev/null
else
  aws secretsmanager create-secret --name "$secret_name" --secret-string "$secret_json" --region "$region" >/dev/null
fi
aws secretsmanager describe-secret --secret-id "$secret_name" --region "$region" --query ARN --output text
