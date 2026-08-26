#!/usr/bin/env bash
set -euo pipefail

# Build a Linux/arm64 Lambda bundle without placing build output in Git.
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
build_dir="$repo_root/build/lambda"
rm -rf "$build_dir"
mkdir -p "$build_dir"

python3 -m pip install \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 314 \
  --only-binary=:all: \
  --target "$build_dir" \
  "aiohttp>=3.11" "boto3>=1.35" "keyring>=25.6" "pylitterbot>=2025.6.2,<2026" "typing-extensions>=4.12"
cp -R "$repo_root/src/lr4_diagnostics" "$build_dir/"
