#!/usr/bin/env bash
# Usage: ci-tested.sh check <artifact-prefix> <workflow-path> | ci-tested.sh tree; writes to $GITHUB_OUTPUT.
set -uo pipefail

repo=repos/$GITHUB_REPOSITORY

tree_sha() {
	gh api "$repo/git/commits/$GITHUB_SHA" --jq .tree.sha
}

tested() {
	local prefix=$1 workflow=$2 tree heads entry
	tree=$(tree_sha) || return 1
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

case ${1:-} in
check)
	if tested "$2" "$3"; then echo tested=true; else echo tested=false; fi >>"$GITHUB_OUTPUT"
	;;
tree)
	tree=$(tree_sha) || exit 1
	echo "sha=$tree" >>"$GITHUB_OUTPUT"
	;;
*)
	echo "usage: ci-tested.sh check <artifact-prefix> <workflow-path> | ci-tested.sh tree" >&2
	exit 2
	;;
esac
