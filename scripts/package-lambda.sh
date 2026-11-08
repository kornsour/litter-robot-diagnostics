#!/usr/bin/env bash
set -euo pipefail

# Build a Linux/arm64 Lambda bundle without placing build output in Git.
#
# Dependencies come from `uv.lock`, not from the version ranges in
# pyproject.toml. Installing the ranges resolved to whatever was newest on the
# day of the build, so the lock file Dependabot keeps current never decided
# what shipped, and a scheduled deploy would have pushed unreviewed releases to
# the one component that sends commands to the device.
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
build_dir="$repo_root/build/lambda"
requirements="$repo_root/build/lambda-requirements.txt"
rm -rf "$build_dir"
mkdir -p "$build_dir"

# uv is required: the deploy workflow installs it with astral-sh/setup-uv.
uv export --project "$repo_root" --frozen --no-dev --no-emit-project --no-hashes \
  --format requirements-txt --output-file "$requirements" >/dev/null

# --no-compile: .pyc files embed source mtimes, so compiling would make every
# build's bundle differ and every deploy a code change even with nothing new.
python3 -m pip install \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 314 \
  --only-binary=:all: \
  --no-compile \
  --no-deps \
  --target "$build_dir" \
  -r "$requirements"
cp -R "$repo_root/src/lr4_diagnostics" "$build_dir/"
find "$build_dir/lr4_diagnostics" -name '__pycache__' -prune -exec rm -rf {} +
