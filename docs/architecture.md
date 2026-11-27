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
| `mounts`, `environment`, `hooks` | Prepare mounts and selected process variables; run host pre-launch Bash |
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

An optional `[hooks].pre_launch` script is sourced in a separate host Bash
process before a new CLI or Desktop process. The default timeout is 60 seconds.
Exports travel through an anonymous NUL-delimited pipe, separate from script
output. Only configured environment names can change in the app's Docker exec
environment; launcher and Docker transport settings keep their original values.
The launcher environment is not modified. The hook runs before mount preparation,
so it can prepare files under configured mounts. Errors or timeouts abort launch
before stopping an existing Desktop. Reusing Desktop skips the hook and credential
validation. Build, check, help, and teardown do not run hooks.

## Images and Compose

Both products use Ubuntu 26.04. The `common` stage provides the mirrored account,
Docker client tools, and the image tool manager. The `cli` stage adds
configured development tools. The `desktop-runtime` stage adds the graphical
dependencies. The `desktop` target adds development tools, runs customization,
then installs the exact Desktop package. The CLI image does not use either
Desktop stage.

The independent `claude-code` stage downloads the selected native Linux
binary and verifies its platform SHA-256 against the exact release manifest.
Both targets copy the root-owned mode `0555` binary to `/usr/local/bin/claude`
after package installation, mise, and customization. The runtime environment sets
`DISABLE_UPDATES=1`. Changing only the selected version changes the artifact
download and final copy, so `build` can reuse package, tool, and hook layers.
`build --rebuild` pulls fresh base images and skips the cache.
`build` and `update` default to both images; `--only-cli` and
`--only-desktop` select one image. `update --rebuild` also skips the cache.

The first launcher build resolves the latest official release and saves its
exact version in the user's XDG configuration directory. Later builds use
that pin. `update` and `update --only-cli` check the Code release
once and keep a newer saved version. A local Python worker requests the official
latest metadata over verified HTTPS. It accepts only a complete HTTP 200
response with a valid numeric version and rejects redirects. The worker sends
validated failure categories to the parent, which reports each error once.
The launcher reports progress on stderr and enforces a 30-second deadline that covers DNS, connection,
and response reads. Each socket operation has a 10-second timeout. Saved pins
bypass the worker. Timeout, cancellation, and metadata errors preserve the
previous pin and prevent the build. Binary downloads during the image build
have separate limits.

One private context contains the exact selected version for both targets.
The pin survives a Docker failure, uninstall, and purge. Raw Docker builds
use the shipped `latest` policy
in `image/claude-code.version`. They resolve it when the artifact layer
runs, which can be skipped on a Docker cache hit.

For Desktop targets, the first launcher build, `update`, or `update --only-desktop`
runs a temporary container on the selected Docker daemon to resolve the native
signed Anthropic apt candidate. The daemon caches an Ubuntu helper keyed by its
recipe, script, and architecture. The helper and Desktop package stage copy the
public CA bundle from the existing mise image and use Ubuntu's built-in apt over
verified HTTPS. No Ubuntu package indexes are downloaded for resolution.
Each lookup fetches the signing key and signed index into fresh storage. The launcher streams progress to stderr and reads only the version from
stdout. Both resolvers own their subprocess groups and defer default
cancellation until cleanup. Docker operations share a 300-second deadline,
with up to 10 seconds to remove the invocation's container. The
launcher stores the version in `claude-desktop.version` beside the Code pin and
snapshots it for the selected build. The independent `claude-desktop-package`
stage downloads exactly that version from the signed stable index, checks its
identity and architecture, and passes the archive to a late networkless `dpkg`
step through a read-only mount.
The runtime layer declares all required Debian dependencies. A new unmet
dependency fails the build with diagnostics from dpkg. Normal builds reuse the
saved selection and the earlier runtime, tool, and customization layers when
only Desktop changes. The raw Docker policy also supports `latest`.

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
The optional example includes Helm, Terraform, Ansible Core, and database/network/build
clients from apt. It requests mise Python 3.14 for locked Ansible; uv stays in the baseline. Init does not modify an existing personal profile.

Personal profiles accept additive `[tools]` and apt-only `[bootstrap.packages]`.
Python packages use the `pipx:` backend with a version string. Other sections,
baseline removal, unsupported managers, and non-Python dependency sidecars are rejected. The program controls locked installs and checksum policy.
`tools lock` runs pinned mise in Docker using only private input snapshots and
filtered Docker transport variables. Matching resolutions remain stable;
`--bump` refreshes them. Separate base and user native locks contain artifacts
and checksums for linux-x64 and linux-arm64. Apt-only profiles need no artifact
lock; apt requests are not artifact-pinned. Comments and apt edits do not
invalidate unchanged tool requests. Contributor `tools lock --base` is limited
to source/development checkouts. Old base-profile edits need manual migration.

Python requests use a separate cached native helper built from official
`python:3.14-slim`, pinned mise, and the baseline-pinned official uv image.
The helper has no host tools or credentials mounted. A helper revision includes
its Dockerfile and daemon platform; reuse validates its label and immutable ID.
Only Python requests use this helper; other locks still use the pinned mise image.
Native `uv.lock` and `pyproject.toml` dependency sidecars are read below the profile
directory without following links. Only referenced files enter snapshots. Native
uv digests are checked; published directories also address both files by SHA-256.
Complete files are fsynced before exclusive publication. Graphs publish first,
then `mise.lock` is atomically replaced after checking unchanged host inputs.
A failed or interrupted resolution keeps the previous lock and graph usable.
Old graph directories are retained to avoid breaking another lock or reader.
Graph directories survive update, uninstall, and purge. Removing the final Python
request leaves old graphs inert; it does not block future locks or builds.
Python sidecars are user-only; the baseline keeps binary artifact locks.

Build never resolves or refreshes locks. It validates and snapshots exact inputs
in a private temporary context shared by both products for `build` or `update`.
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
mounted HOME and installs Google Cloud CLI from `packages.cloud.google.com`.
Its dedicated apt keyring is scoped to that repository with `signed-by`.
Neither example changes the shipped baseline.

Without arguments the launcher prints full help to stdout, without resolving
configuration paths, parsing TOML, creating state, accessing Docker, or resolving
application releases. Config/build-input paths are reported only with debug.
The global `-d`/`--debug` prefix reports the same paths before parsing, including
when help/version actions exit early. Their normal stdout remains unchanged;
ordinary commands do not list them. Missing optional files are marked. These describe current host inputs, which can differ from the inputs
used for an existing image. Starts always query actual container versions:
Code uses `claude --version`; Desktop uses the installed package version from
`dpkg-query`, without starting Electron. Each probe has a five-second deadline,
filtered Docker transport environment and no forwarded application credentials.
Probe failure prints `unavailable` and does not block the application launch.

`compose.yaml` defines the CLI and Desktop services. The launcher supplies build
identity and a runtime override with validated mounts and selected settings.
Desktop is a separate Compose profile. Resource labels record project ownership
and host UID.

A nonblocking lock for the host UID's Compose project covers launcher startup,
Desktop reuse and restart, and teardown. The lock lives in a private
`.claude-docker-locks` directory under the host account's home, as recorded in
the account database, so it coordinates launches with different HOME or XDG
paths. A competing command exits with a retry diagnostic
before running launch hooks or changing runtime state. The lock is released
before interactive Claude Code execution. The next launch can recover a pending
mode after the process that recorded it exits.

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

## Nested Desktop display

Xephyr enables window resizing and disables RANDR so that core X11 geometry
changes reach the application. The default keyboard layouts are `us,ru`, with
`grp:alt_shift_toggle` to switch layouts inside the nested display. These settings
do not change the host keyboard layout.

Set `keyboard_layout` and `keyboard_options` in `[desktop]`, or use
`CLAUDE_DESKTOP_KEYBOARD_LAYOUT` and `CLAUDE_DESKTOP_KEYBOARD_OPTIONS`, to override
the defaults. `desktop --restart` applies changed settings. Reusing a running
Desktop keeps its current keyboard state.

Startup applies the keymap before publishing reusable runtime state. Xephyr
verification tolerates individual file descriptors that close during inspection,
while still requiring the recorded process to own the display listener socket.

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

Disposable-container checks verified that the application follows Xephyr window
size changes. Real image builds verified reuse of user mise and customization
layers when only the selected Claude versions changed. These checks used a warm
build cache; they are not cold-build benchmarks. Physical keyboard switching,
host-specific GUI behavior, and broader Sysbox qualification remain manual checks.
