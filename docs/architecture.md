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

Both products use Ubuntu 24.04. The `common` stage provides the mirrored account,
native Claude Code, Docker client tools, and the image tool manager. The `cli`
stage adds configured development tools. The `desktop-runtime` stage adds the
Desktop package and graphical dependencies; the `desktop` target adds development
tools. The CLI image does not inherit the Desktop stage.

`image/mise.toml`, `image/mise.lock`, and `image/customize.sh` define image tools.
Mise runs inside Docker. The Docker build context admits only the Dockerfile and
these three image inputs.

`compose.yaml` defines the CLI and Desktop services. The launcher supplies build
identity and a runtime override with validated mounts and selected settings.
Desktop is a separate Compose profile. Resource labels record project ownership
and host UID.

`docker_mode = "none"` is the default. The optional `compose.sysbox.yaml` overlay
provides a separate inner Docker daemon, mutual TLS, and named storage. It needs
a separately installed Sysbox runtime. The host Docker socket is not mounted.
No service uses privileged mode. Ubuntu 24.04 is the intended Sysbox reference
host; Debian 13 use is experimental and live Sysbox qualification remains open.

## Installation and removal

The `install` entry point supports user-local releases, a development link, and
a shared system installation. User releases are stored under
`~/.local/share/claude-docker/releases`; the command link is under `~/.local/bin`.
Installed bundles are immutable. Explicit file inventories and integrity checks
control updates and removal. Source checkouts are used for image customization.

Normal uninstall retains configuration, user data, images, networks, and volumes.
Purge requires consent and validates exact managed resource names and labels.
Workspaces and Claude state sources are protected in all removal modes. Pending
removal records support retry after interruption. Do not edit these records.

## Verification

Run the normal unit suite from the source root:

```bash
python3 -m unittest discover -v
```

Live Docker, Sysbox, and Desktop tests require explicit environment flags. Keep
these flags unset for the normal suite. See [Desktop security](desktop-security.md)
for the opt-in seccomp test and its limits. Documentation and local development
records are excluded from installed bundles and image build inputs.
