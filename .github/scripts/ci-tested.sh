#!/usr/bin/env bash
# Usage: ci-tested.sh <artifact-prefix> <workflow-path>; writes tested=true|false to $GITHUB_OUTPUT.
set -uo pipefail

prefix=$1
workflow=$2
repo=repos/$GITHUB_REPOSITORY

tested() {
	local tree heads entry
	tree=$(gh api "$repo/git/commits/$GITHUB_SHA" --jq .tree.sha) || return 1
	heads=$(gh api "$repo/commits/$GITHUB_SHA/pulls" --jq '.[] | select(.merged_at) | .head.sha') || return 1
	[[ -n $heads ]] || return 1
	for entry in $(gh api "$repo/actions/artifacts?name=$prefix-tested-$tree" \
		--jq '.artifacts[] | select(.expired | not) | "\(.workflow_run.id):\(.workflow_run.head_sha)"'); do
		# Only trust a run of the merged PR itself, so another PR cannot plant a matching artifact.
		grep -qxF "${entry#*:}" <<<"$heads" || continue
		[[ $(gh api "$repo/actions/runs/${entry%%:*}" --jq '[.event, .conclusion, .path] | join(" ")') \
			== "pull_request success $workflow" ]] && return 0
	done
	return 1
}

if tested; then echo tested=true; else echo tested=false; fi >>"$GITHUB_OUTPUT"
