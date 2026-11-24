FROM ubuntu:24.04

ARG RUNNER_VERSION=2.338.2
ARG RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
ARG GH_VERSION=2.103.3
ARG GH_SHA256=e3bafef96328d1a67de3a968bdfe1cc2ffd58cf6aba8b5e201356897ed06bb3d

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_DIR=/home/runner/actions-runner \
    ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/job-started.sh

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates coreutils curl git gnupg jq less procps sudo tar unzip wget xz-utils zip zstd \
        build-essential make python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,source=common/install-docker.sh,target=/tmp/install-docker.sh \
    bash /tmp/install-docker.sh

RUN curl -fsSL -o /tmp/gh.deb \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.deb" \
    && echo "${GH_SHA256}  /tmp/gh.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/gh.deb \
    && rm -rf /tmp/gh.deb /var/lib/apt/lists/*

RUN userdel --remove ubuntu \
    && useradd --create-home --uid 1000 --shell /bin/bash --groups docker runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner

RUN install -d -o runner -g runner "${RUNNER_DIR}" \
    && curl -fsSL -o /tmp/runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && echo "${RUNNER_SHA256}  /tmp/runner.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/runner.tar.gz -C "${RUNNER_DIR}" \
    && rm /tmp/runner.tar.gz \
    && chown -R runner:runner "${RUNNER_DIR}" \
    && "${RUNNER_DIR}/bin/installdependencies.sh" \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 common/dockerd.sh /usr/local/lib/skeleton-crew/dockerd.sh
COPY --chmod=0755 runner/entrypoint.sh runner/job-started.sh /usr/local/bin/

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
