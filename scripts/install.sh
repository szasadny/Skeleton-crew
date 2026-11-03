#!/usr/bin/env bash
set -euo pipefail

SYSBOX_VERSION=0.7.1
SYSBOX_SHA256=9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5
APP_UID=1000

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

die() {
	echo "install.sh: $*" >&2
	exit 1
}

env_value() {
	local value
	value="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)"
	value="${value#\'}"
	value="${value%\'}"
	value="${value#\"}"
	value="${value%\"}"
	printf '%s' "$value"
}

set_env_if_missing() {
	if ! grep -q "^$1=." "$ENV_FILE"; then
		sed -i "/^$1=/d" "$ENV_FILE"
		printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
	fi
}

compose() {
	docker compose --project-directory "$REPO_DIR" "$@"
}

check_host() {
	[[ $EUID -eq 0 ]] || die "run as root (sudo)"
	# shellcheck source=/dev/null
	. /etc/os-release
	case "${ID:-} ${VERSION_ID:-}" in
	"ubuntu 24.04" | "debian 12") ;;
	*) die "only Ubuntu 24.04 and Debian 12 are supported" ;;
	esac
	[[ $(dpkg --print-architecture) == amd64 ]] || die "only amd64 is supported"
}

install_docker() {
	apt-get update
	# fuse3 provides fusermount3, which sysbox-fs needs and minimal Debian lacks.
	apt-get install -y ca-certificates curl fuse3 jq rsync
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	cat >/etc/apt/sources.list.d/docker.sources <<-EOF
		Types: deb
		URIs: https://download.docker.com/linux/${ID}
		Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
		Components: stable
		Signed-By: /etc/apt/keyrings/docker.asc
	EOF
	apt-get update
	apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
	systemctl enable --now docker
}

install_sysbox() {
	local installed tmp deb
	installed="$(dpkg-query -W -f='${Version}' sysbox-ce 2>/dev/null || true)"
	if [[ $installed != "$SYSBOX_VERSION"* ]]; then
		tmp="$(mktemp -d)"
		deb="$tmp/sysbox-ce_${SYSBOX_VERSION}.linux_amd64.deb"
		curl -fsSLo "$deb" \
			"https://github.com/nestybox/sysbox/releases/download/v${SYSBOX_VERSION}/sysbox-ce_${SYSBOX_VERSION}.linux_amd64.deb"
		echo "$SYSBOX_SHA256  $deb" | sha256sum -c -
		# The Sysbox installer restarts Docker and refuses while containers are running.
		if [[ -n $(docker ps -q) ]]; then
			if [[ -f $ENV_FILE ]]; then
				compose down
			fi
			[[ -z $(docker ps -q) ]] || die "stop all running containers, then rerun"
		fi
		chmod 0755 "$tmp"
		chmod 0644 "$deb"
		apt-get install -y "$deb"
		rm -rf "$tmp"
	fi
	systemctl is-active --quiet sysbox-mgr sysbox-fs || die "sysbox services are not running (journalctl -u sysbox-mgr)"
	docker info --format '{{json .Runtimes}}' | grep -q '"sysbox-runc"' \
		|| die "sysbox-runc is not registered as a Docker runtime"
}

prepare_env() {
	if [[ ! -f $ENV_FILE ]]; then
		install -m 0600 "$REPO_DIR/.env.example" "$ENV_FILE"
	fi
	set_env_if_missing DOCKER_GID "$(getent group docker | cut -d: -f3)"
	set_env_if_missing SC_SITE_ADDRESS "$(hostname -I | awk '{print $1}')"
}

prepare_data_dir() {
	local data_dir
	data_dir="$(env_value SC_DATA_DIR)"
	data_dir="${data_dir:-/var/lib/skeleton-crew}"
	install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "$data_dir" "$data_dir/tasks"
	install -d -m 0755 -o "$APP_UID" -g "$APP_UID" "$data_dir/egress"
	if [[ ! -f $data_dir/egress/allowlist.txt ]]; then
		install -m 0644 -o "$APP_UID" -g "$APP_UID" /dev/null "$data_dir/egress/allowlist.txt"
	fi
}

is_configured() {
	[[ $(env_value SC_REPO) != "" && $(env_value SC_REPO) != "owner/name" ]] \
		&& [[ -n $(env_value SC_UI_PASSWORD_HASH) ]] \
		&& [[ -n $(env_value SC_SESSION_SECRET) ]] \
		&& [[ -f $(env_value SC_GITHUB_APP_PRIVATE_KEY_PATH) ]]
}

main() {
	check_host
	install_docker
	install_sysbox
	prepare_env
	prepare_data_dir
	compose --profile build build
	if is_configured; then
		compose up -d
		echo "Skeleton Crew is running: https://$(env_value SC_SITE_ADDRESS)"
		echo "Keep dry-run on for the first runs and read the audit log."
	else
		echo "Images built. Next steps (see docs/install.md):"
		echo "  1. Fill in $ENV_FILE and place the GitHub App key at SC_GITHUB_APP_PRIVATE_KEY_PATH."
		echo "  2. docker compose run --rm controller skeleton-crew hash-password"
		echo "  3. Put the hash in SC_UI_PASSWORD_HASH (single quotes), then rerun: sudo scripts/install.sh"
	fi
}

main "$@"
