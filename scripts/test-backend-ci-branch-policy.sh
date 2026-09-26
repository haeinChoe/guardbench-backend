#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend_ci="$repo_root/.github/workflows/backend-ci.yml"
worker_sync="$repo_root/.github/workflows/staging-worker-image-sync.yml"

require() {
  local file="$1"
  local pattern="$2"
  local description="$3"
  if ! grep -Eq "$pattern" "$file"; then
    echo "missing branch policy: $description" >&2
    exit 1
  fi
}

require "$backend_ci" '^  pull_request:$' 'pull_request trigger'
require "$backend_ci" '^    branches: \[dev, main\]$' 'PR verification on dev and main'
require "$backend_ci" '^  push:$' 'push trigger'
require "$backend_ci" '^    branches: \[dev, main\]$' 'verification on dev and main pushes'
require "$backend_ci" "github\.ref == 'refs/heads/dev' \|\| github\.ref == 'refs/heads/main'" 'verification scope on dev and main pushes'
require "$backend_ci" "github\.ref == 'refs/heads/main'" 'automatic deployment restricted to main'
require "$backend_ci" "needs\.changes\.outputs\.application_source == 'true'" 'automatic deployment requires application source changes'
require "$backend_ci" "github\.event_name == 'workflow_dispatch' && github\.ref == 'refs/heads/dev'" 'manual deployment remains available from dev'
require "$backend_ci" 'staging\|dev\|performance' 'staging and manual deployment targets'
require "$backend_ci" 'needs\.verify\.result == .success.' 'deployment requires successful verification'
require "$worker_sync" '^name: Staging Worker Image Sync$' 'staging worker sync workflow'
require "$worker_sync" '^    branches: \[main\]$' 'worker sync only follows main workflow runs'
require "$worker_sync" 'deploy staging.*conclusion == .success.' 'worker sync requires successful staging deployment'
require "$worker_sync" '^    environment: staging$' 'worker sync uses staging Environment'

echo 'Backend CI branch policy tests passed'
