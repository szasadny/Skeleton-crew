FROM ubuntu:24.04

ARG CLAUDE_CODE_VERSION=2.1.312
ARG DOCKER_GPG_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
ARG MISE_VERSION=2026.11.1
ARG MISE_SHA256=7129dbab31d698f9028db9374f7ba7987232b0426f837dcbce437130a61415f9

ENV DEBIAN_FRONTEND=noninteractive \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    MISE_DATA_DIR=/home/agent/.local/share/mise \
    MISE_YES=1 \
    MISE_TRUSTED_CONFIG_PATHS=/workspace \
    MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS=python,node,ruby,go,java \
    PATH=/home/agent/.local/share/mise/shims:${PATH}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates coreutils curl git gnupg less procps ripgrep \
        build-essential make python3 python3-pip python3-venv nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc \
        | grep -q "^fpr:::::::::${DOCKER_GPG_FINGERPRINT}:" \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/mise \
        "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64" \
    && echo "${MISE_SHA256}  /tmp/mise" | sha256sum -c - \
    && install -m 0755 /tmp/mise /usr/local/bin/mise \
    && rm /tmp/mise \
    && mise --version

# The native installer targets $HOME; the binary is self-contained, so it is moved to a system path.
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && bash /tmp/install-claude.sh "${CLAUDE_CODE_VERSION}" \
    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /tmp/install-claude.sh /root/.local /root/.claude /root/.claude.json \
    && claude --version

RUN userdel --remove ubuntu \
    && useradd --create-home --uid 1000 --shell /bin/bash --groups docker agent \
    && git config --system safe.directory /workspace \
    && git config --system user.name "Skeleton Crew Agent" \
    && git config --system user.email "agent@skeleton-crew.invalid" \
    && install -d -o agent -g agent /workspace

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
