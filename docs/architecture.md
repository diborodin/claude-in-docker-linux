# Architecture

This document describes the current implementation. Use the [user manual](../README.md)
for installation and commands. Use [config.example.toml](../config.example.toml)
for configuration keys.

## Host launcher

The host command uses Python 3.11 or newer and the standard library. It calls
Docker Engine and Docker Compose v2. It needs no host mise, pip packages, or jq.
The launcher does not install host packages or change the Docker daemon.

The root `claude-docker` executable loads `src/claude_docker`. The main modules are:

| Modules | Responsibility |
| --- | --- |
| `cli`, `commands`, `process` | Parse commands and run child processes |
| `config`, `model`, `paths` | Validate configuration and resolve host paths |
| `mounts`, `environment` | Prepare state mounts and select process variables |
| `compose`, `state` | Generate runtime overrides and track managed resources |
| `desktop`, `clipboard`, `seccomp` | Manage the nested display and Desktop policy |
| `installer`, `manifest`, `uninstall` | Install, verify, update, and remove program files |
| `tool_config`, `image_tools` | Validate personal tool profiles, lock artifacts, snapshot build inputs |
| `checks` | Report prerequisites without starting workloads, unless smoke is requested |

## Configuration and host files

Process overrides take precedence over TOML. TOML takes precedence over detected
defaults. The launcher detects the host account, UID, primary GID, and home.
Configuration can override the container identity. The container account must
not be root. Desktop requires the container UID to match the host UID.

Each configured workspace mounts at the same absolute path inside the container.
Workspace roots must exist. This preserves existing project paths. An empty list
disables workspace mounts.

Claude state has three independent mounts:

| Host default | Preparation |
| --- | --- |
| `~/.claude` | Create a missing directory with mode 0700 |
| `~/.config/Claude` | Create a missing directory with mode 0700 |
| `~/.claude.json` | Mount an existing regular file; do not create a missing file |

Each source can be changed or its mount disabled. Unmounted state is local to the
container and is lost when that container is removed. Mounts grant write access
to host data. Keep workspace roots narrow.

The environment configuration contains variable names, not credential values.
Required names must have nonempty values. Optional names are forwarded when
present. Each new process receives the selected current host values. The launcher
does not put these values in generated Compose files or process arguments.
Desktop must restart to receive refreshed values. Process environments remain
readable to the application, the Docker daemon, and permitted process inspectors.

## Images and Compose

Both products use Ubuntu 26.04. The `common` stage provides the mirrored account,
Docker client tools, and the image tool manager. The `cli` stage adds
configured development tools. The `desktop-runtime` stage adds the
Desktop package and graphical dependencies; the `desktop` target adds development
tools. The CLI image does not inherit the Desktop stage.

The independent `claude-code` stage downloads the selected native Linux
binary and verifies its platform SHA-256 against the exact release manifest.
Both targets copy the root-owned mode `0555` binary to `/usr/local/bin/claude`
after package installation, mise, and customization. The runtime environment sets
`DISABLE_UPDATES=1`. Changing only the selected version changes the artifact
download and final copy, so `build` can reuse package, tool, and hook layers.
`rebuild` skips the cache.

The first launcher build resolves the latest official release and saves its
exact version in the user's XDG configuration directory. Later builds use
that pin. `build --update-claude` and `rebuild --update-claude` check the release
once and keep a newer saved version. One private context contains the exact
selected version for both targets. The pin survives a Docker failure,
uninstall, and purge. Raw Docker builds use the shipped `latest` policy
in `image/claude-code.version`. They resolve it when the artifact layer
runs, which can be skipped on a Docker cache hit.

The shipped `image/mise.toml`, `image/mise.lock`, and `image/customize.sh` define
the baseline. Apt installs ripgrep, jq, shellcheck, shfmt, sd, miller, hyperfine,
bats, less, and bsdextrautils. Mise installs fd, bat, ast-grep, yq, gh, uv,
difftastic, scc, and duckdb with checksums for both architectures. Kubectl is optional.
`tools init` explicitly creates a private, non-overwriting user example at
`~/.config/claude-docker/mise.toml` (respecting `XDG_CONFIG_HOME`).
`CLAUDE_MISE_CONFIG_FILE` selects an absolute or `~/` source independently of
launcher configuration; its suffix is replaced by `.lock` for the sibling lock.
Missing default profiles add no declarative tools. Missing explicit sources are errors
for tool/build operations, but do not prevent teardown.

Personal profiles accept additive `[tools]` and apt-only `[bootstrap.packages]`.
Other sections, baseline removal, unsupported managers, and dependency sidecars
are rejected. The program controls locked installs and checksum policy.
`tools lock` runs pinned mise in Docker using only private input snapshots and
filtered Docker transport variables. Matching resolutions remain stable;
`--bump` refreshes them. Separate base and user native locks contain artifacts
and checksums for linux-x64 and linux-arm64. Apt-only profiles need no artifact
lock; apt requests are not artifact-pinned. Comments and apt edits do not
invalidate unchanged tool requests. Contributor `tools lock --base` is limited
to source/development checkouts. Old base-profile edits need manual migration.

Build never resolves or refreshes locks. It validates and snapshots exact inputs
in a private temporary context shared by both products for `build --all`.
The Dockerfile and filtered image inputs are the only admitted files; no whole
HOME, configuration directory, or workspace is sent to Docker. Both products
install baseline and user tools in system paths outside mounted HOME.

`customize init` copies the shipped `image/customize.example.sh` into the
invoking user's `~/.config/claude-docker/customize.sh` (respecting
`XDG_CONFIG_HOME`). A build includes only the exact bytes of the selected
script as `image/customize-user.sh`. Both targets run it as root with Bash
strict mode after package setup, mise install, shim creation, and the baseline
hook. A failing script fails the build. An absent script skips this step.
The example extracts `checkbashisms` into `/usr/local/bin` for use with a
mounted HOME.

`compose.yaml` defines the CLI and Desktop services. The launcher supplies build
identity and a runtime override with validated mounts and selected settings.
Desktop is a separate Compose profile. Resource labels record project ownership
and host UID.

`docker_mode = "none"` is the default. The optional `compose.sysbox.yaml` overlay
provides a separate inner Docker daemon, mutual TLS, and named storage. It needs
a separately installed Sysbox runtime. The host Docker socket is not mounted.
No service uses privileged mode. Ubuntu 24.04 is the intended Sysbox reference
host; Debian 13 use is experimental and live Sysbox qualification remains open.

Desktop waits for `inner-docker` with a service-only Compose start that has a
120-second health timeout. Dependency repair preserves existing containers and
uses the base and Sysbox models without runtime grants. Startup and reuse
wait before changing the Desktop session. Ordinary Desktop startup handles
any required mode recovery first, then starts Desktop with dependencies excluded
from that Compose operation. Desktop-only restart still requires a compatible
recorded mode and no pending transition.

The optional Chromium policy uses the outer Docker server GitCommit to resolve
an exact published official Moby release tag for the daemon version, then fetch the
profiles dependency at its published tag, baseline and license over HTTPS. The private
cache verifies the baseline, derivation and provenance on reuse. Desktop launch
uses a new cache key for this resolution scheme. Keep the real cache path free of
symlink ancestors. Desktop compares the requested policy with the running container before reuse.

## Installation and removal

The `install` entry point supports user-local releases, a development link, and
a shared system installation. User releases are stored under
`~/.local/share/claude-docker/releases`; the command link is under `~/.local/bin`.
Installed bundles are immutable. Explicit file inventories and integrity checks
control updates and removal. Personal tool workflows run from installed bundles;
advanced `image/customize.sh` edits require a source checkout.

Normal uninstall retains configuration, user data, images, networks, and volumes.
Purge requires consent and validates exact managed resource names and labels.
Workspaces, Claude state sources, user tool profiles and locks, and the personal
build script survive updates, normal uninstall, and purge. These files live
outside release manifests. Teardown derives tool and script paths without
reading their contents and protects lexical and resolved aliases. Pending
removal records retain that protected inventory across retries. They
support retry after interruption. Do not edit these records.

## Verification

Run the normal unit suite from the source root:

```bash
python3 -m unittest discover -v
```

Live Docker, Sysbox, and Desktop tests require explicit environment flags. Keep
these flags unset for the normal suite. See [Desktop security](desktop-security.md)
for the opt-in seccomp test and its limits. Documentation and local development
records are excluded from installed bundles and image build inputs.
