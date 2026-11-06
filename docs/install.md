# Install

Skeleton Crew runs on one dedicated Ubuntu 24.04 or Debian 12 VM. The controller holds the Docker socket, so the VM must hold nothing else.

## 1. Create the VM (Proxmox)

1. Download the Ubuntu 24.04 cloud image (`noble-server-cloudimg-amd64.img`) or the Debian 12 netinst ISO and create a new VM from it.
2. Give it 10 vCPU, 24 GB RAM and a 60 GB disk (see Sizing below for a smaller VM). Nested virtualisation is not needed: Sysbox runs inside the guest.
3. Add a cloud-init drive with your user, SSH key and a static LAN IP, then boot.
4. Clone this repository on the VM, for example to `/opt/skeleton-crew`.

### Sizing

A worker keeps its reviewer container alive between review rounds, and the refiner runs alongside the workers; the selector finishes before any worker starts. The peak is `2 × max_parallel_workers + 1` agent containers, each capped at `cpus` and `memory_gb` (Repo page), plus the controller and the egress proxy.

The caps are limits, not reservations: a container only uses what it needs, up to its cap. With the defaults (2 workers, 2 CPUs, 4 GB) the peak is 5 containers with caps of 10 CPUs and 20 GB, so 10 vCPU and 24 GB RAM run the defaults at full load. For a smaller VM, lower `max_parallel_workers` (Limits page) or `cpus` and `memory_gb` (Repo page) until the peak fits; 1 worker with 1 CPU and 2 GB fits 4 vCPU and 8 GB.

## 2. Create the GitHub App

1. Create a new GitHub App under your account or organisation, either by hand or from [github-app-manifest.json](github-app-manifest.json) (set a unique `name` and your `url` first).
2. Set exactly these repository permissions:

   | Permission | Access |
   | --- | --- |
   | Contents | Read and write |
   | Issues | Read and write |
   | Pull requests | Read and write |
   | Checks | Read and write |
   | Actions | Read-only |
   | Commit statuses | Read-only |
   | Metadata | Read-only |

   Do not grant Workflows or Administration. Preflight refuses to run if the App has Workflows or Administration write.
3. Disable the webhook and keep the App private.
4. Install the App on the one target repository only. Note the App ID and, optionally, the installation ID.
5. Generate a private key and copy it to the VM:

   ```sh
   sudo install -d -m 0755 /etc/skeleton-crew
   sudo install -m 0400 -o 1000 -g 1000 github-app.pem /etc/skeleton-crew/github-app.pem
   ```

## 3. Protect the base branch

In the target repository, add a ruleset for the base branch (usually `main`):

1. Require a pull request before merging, with at least 1 approval.
2. Do not add the App to the bypass list. Preflight refuses to run if the App can bypass the ruleset.
3. Recommended: block force pushes and block deletions.

Rulesets need a public repository or a paid plan. On a free private repository, skip this step and enable **Repo → Allow unprotected base branch** in the UI after install; read the trade-off in [SECURITY.md](../SECURITY.md) first.

## 4. Install

1. Run the installer. It installs Docker CE and Sysbox, creates the data directory, writes `.env` from `.env.example` and builds the images:

   ```sh
   cd /opt/skeleton-crew
   sudo scripts/install.sh
   ```

2. Fill in `.env`:
   - `SC_REPO`: the target repository as `owner/name`.
   - `SC_GITHUB_APP_ID`, `SC_GITHUB_APP_PRIVATE_KEY_PATH` and optionally `SC_GITHUB_APP_INSTALLATION_ID`, `SC_GITHUB_BOT_LOGIN`.
   - The agent credential, as `SC_AGENT_CREDENTIALS__<NAME>`. For Claude Code: `SC_AGENT_CREDENTIALS__CLAUDE_OAUTH_TOKEN` (from `claude setup-token`, auth mode `subscription`) or `SC_AGENT_CREDENTIALS__ANTHROPIC_API_KEY` (auth mode `api_key`, chosen on the Agents page). Use a dedicated token.
   - `SC_SESSION_SECRET`: output of `openssl rand -hex 32`.
   - `SC_SITE_ADDRESS`: the VM's LAN IP or hostname (the installer fills in the first IP it finds).
3. Create the UI password hash and put it in `SC_UI_PASSWORD_HASH`, inside single quotes:

   ```sh
   sudo docker compose run --rm controller skeleton-crew hash-password
   ```

4. Rerun `sudo scripts/install.sh`. With a complete `.env` it starts the stack.

## 5. First login

1. Open `https://<vm-ip>`. Caddy uses its own internal CA, so accept the certificate warning or trust Caddy's root certificate.
2. Log in with the password from step 4.3.
3. Check the setup. This reports problems with the ruleset, App permissions, agent settings and credentials, Sysbox, images and the egress proxy:

   ```sh
   sudo docker compose exec controller skeleton-crew preflight
   ```

## 6. Dry run, then go live

1. Leave `dry_run` on (the default) for the first runs. Every GitHub write is then recorded in the audit log as dry-run instead of performed. Agent sessions still run (and cost tokens); CI is skipped.
2. Label a few small issues, start a run and read the audit log to see what would have happened.
3. When the planned actions look right, switch `dry_run` off in the UI.

## Toolchains

The agent image ships [mise](https://mise.jdx.dev), so a repo can get the language versions it declares (`.tool-versions`, `mise.toml`, `.python-version`, `.nvmrc`, `.node-version`, …) instead of the image's system Python and Node:

1. On the Repo page, set **Setup command** to `mise install` followed by the repo's dependency install, e.g. `mise install && mise use -g uv@latest && uv sync --locked`. `mise install` only installs the tools the repo declares; add tools like uv with `mise use -g`.
2. Add the hosts mise downloads from to the egress allowlist on the same page. For Python, Node and uv: `mise-versions.jdx.dev`, `nodejs.org`, `github.com`, `release-assets.githubusercontent.com` and `tuf-repo-cdn.sigstore.dev` (attestation checks). Other tools may need more; the egress proxy log shows denied hosts.
3. The target repo must gitignore dependencies installed inside it (`.venv`, `node_modules`), or they end up in the patch.

The setup command runs in the worker's container before its first session, and everything it leaves behind stays available to the worker. That includes containers on the container's own Docker, so it can start services the repo's tests need, e.g. `docker compose -f docker-compose.test.yml up -d --wait db`. Image pulls need `registry-1.docker.io`, `auth.docker.io` and `production.cloudfront.docker.com` in the allowlist (the default). Allowlist changes take effect at the start of the next run.

## Self-hosted CI

Optionally, Skeleton Crew runs the target repo's GitHub Actions jobs on runners on this VM, so CI costs no Actions minutes. Checks, logs and artifacts still land in GitHub. Only private repositories qualify: a public repo would run fork PR jobs on this host. GitHub's "Require approval for all external contributors" setting would cover that, but checking it needs the App permission Administration: read, which the App deliberately does not have; without that check Skeleton Crew would have to trust the setting blindly.

Each runner is a container with Sysbox and its own Docker, so jobs can use `docker`, `docker compose` and service containers, and two jobs that bind the same port do not collide. After every job the container is recreated from the image with the same registration. Runners never get the GitHub App key, a GitHub token or the Docker socket; their traffic goes through their own egress proxy with its own allowlist.

### Register the runners (once)

1. In the target repo, open Settings → Actions → Runners → New self-hosted runner and copy the registration token (valid for one hour; one token can register several runners).
2. On the VM, for each runner `runner-1` … `runner-<n>` (the count on the CI page):

   ```sh
   sudo install -d -o 1000 -g 1000 /var/lib/skeleton-crew/runners/runner-1
   sudo docker run --rm -v /var/lib/skeleton-crew/runners/runner-1:/runner-config \
     skeleton-crew-runner:latest register \
     --url https://github.com/<owner>/<repo> --token <token> \
     --labels skeleton-crew --name skeleton-crew-runner-1
   ```

   This runs `config.sh --unattended --disableupdate` and writes `.runner`, `.credentials` and `.credentials_rsaparams` to that directory. Treat them as a secret: they are the runner's identity. Use the same labels as on the CI page.
3. On the CI page, enable **Run self-hosted runners**. The runners show up as idle there and in GitHub.

### Use them

Skeleton Crew never edits workflows. A workflow opts in with `runs-on` and the label, and chooses its own fallback. To run only Skeleton Crew's PRs on these runners and everything else on GitHub: `runs-on: ${{ startsWith(github.head_ref, 'skeleton/') && vars.CI_RUNNER || 'ubuntu-latest' }}` with the repository variable `CI_RUNNER=skeleton-crew`. `github.head_ref` is only set for pull requests, so pushes and manual runs stay on GitHub.

The image has git, curl, jq, gh, Python, build tools, Docker and passwordless sudo, but far less than GitHub's hosted images. Actions such as `setup-node` and `setup-uv` download their tools. Hosts a job needs go on the CI page's runner allowlist; denied hosts show in `sudo docker logs skeleton-crew-runner-proxy`. Containers a job starts get the HTTPS proxy too, but not an HTTP one, so plain-HTTP downloads from the internet fail inside them; list the hosts they reach directly over HTTPS (e.g. docker compose service names) under **No proxy**. Prefer exact hosts over wildcards for storage domains (`*.r2.cloudflarestorage.com`, `*.blob.core.windows.net`): a wildcard lets a job send data to any bucket.

### Maintenance

- **Updates**: runners do not update themselves. GitHub stops sending jobs to a runner that is too old, so pull and rerun `sudo scripts/install.sh` regularly; the image pins the runner version.
- **Kill switch and off**: both remove the runner containers. Jobs then wait in GitHub until a runner is back.
- **Deregister**: remove the runner in Settings → Actions → Runners, then delete its directory under `/var/lib/skeleton-crew/runners/`. If a registration may have leaked, do this at once and register a new one.
- **Sizing**: each runner is capped at the CI page's `cpus` and `memory_gb`, on top of the agent containers (see Sizing above).

## Labels

How the `skeleton`, `skeleton refine` and `skeleton HITL` labels work: [labels.md](labels.md).

## Updating

```sh
cd /opt/skeleton-crew
git pull
sudo scripts/install.sh
```

Before using it on a real project, run the checks in [validation.md](validation.md).
