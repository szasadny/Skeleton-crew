#!/usr/bin/env bash
set -euo pipefail

prefix="${SC_BRANCH_PREFIX:-}"
branch="${GITHUB_HEAD_REF:-${GITHUB_REF_NAME:-}}"

if [[ -z "$prefix" ]]; then
    echo "::error title=Not a Skeleton Crew runner::SC_BRANCH_PREFIX is not set, so this runner was not started by Skeleton Crew."
    exit 1
fi
if [[ "$branch" == "$prefix"* ]]; then
    exit 0
fi

fix="runs-on: \${{ startsWith(github.head_ref, '${prefix}') && vars.CI_RUNNER || 'ubuntu-latest' }}"
echo "::error title=Not a Skeleton Crew job::These self-hosted runners only run jobs that Skeleton Crew sent (branches starting with '${prefix}'), but this job is for '${branch:-an unknown branch}'. Fix: route only Skeleton Crew pull requests here, e.g. ${fix}"
exit 1
