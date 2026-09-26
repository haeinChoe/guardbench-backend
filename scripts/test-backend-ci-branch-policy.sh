#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend_ci="$repo_root/.github/workflows/backend-ci.yml"

require() {
  local file="$1"
  local pattern="$2"
  local description="$3"
  if ! grep -Eq "$pattern" "$file"; then
    echo "missing branch policy: $description" >&2
    exit 1
  fi
}

reject() {
  local file="$1"
  local pattern="$2"
  local description="$3"
  if grep -Eq "$pattern" "$file"; then
    echo "forbidden branch policy: $description" >&2
    exit 1
  fi
}

require "$backend_ci" '^  pull_request:$' 'pull_request trigger'
require "$backend_ci" '^    branches: \[dev, main\]$' 'PR verification on dev and main'
require "$backend_ci" '^  push:$' 'push trigger'
require "$backend_ci" '^    branches: \[dev, main\]$' 'verification on dev and main pushes'
require "$backend_ci" '^  workflow_dispatch:$' 'manual workflow trigger'
require "$backend_ci" "github\.event_name == 'workflow_dispatch'" 'manual verification path'
reject "$backend_ci" '^    inputs:' 'manual deployment inputs'
reject "$backend_ci" 'inputs\.' 'manual deployment input references'
require "$backend_ci" 'needs\.test_fast\.result' 'verify aggregates testFast'
require "$backend_ci" 'needs\.integration_test\.result' 'verify aggregates integrationTest'
require "$backend_ci" 'needs\.package\.result' 'verify aggregates bootJar'
reject "$backend_ci" '^  deploy:' 'deployment job'
reject "$backend_ci" '(^|[[:space:]])aws([[:space:]]|$)|aws-actions|AWS_|OIDC|id-token: write|ECR|ECS|environment:' 'AWS access or deployment environment'

for worker_sync in "$repo_root"/.github/workflows/*worker-image-sync.yml; do
  if [[ -e "$worker_sync" ]]; then
    echo "forbidden branch policy: worker image sync workflow is enabled: $worker_sync" >&2
    exit 1
  fi
done

if [[ -e "$repo_root/scripts/verify-ecs-deployment.sh" || -e "$repo_root/scripts/test-verify-ecs-deployment.sh" ]]; then
  echo 'forbidden deployment-only ECS verification scripts remain' >&2
  exit 1
fi

echo 'Backend CI branch policy tests passed'
