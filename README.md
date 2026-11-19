# Claude in Docker for Linux

## What the program does

Run Claude Code CLI in a Docker container. You can also run Claude Desktop
in a private Xephyr window. Inner Docker is optional and disabled by default.
The launcher detects your host account, UID, GID, and home directory.

- **host**: Your Linux computer and its Docker daemon.
- **container**: An environment that Docker starts on the host.
- **workspace**: A host directory that contains your working files.
- **mount**: A connection that exposes a host path inside a container.
- **inner Docker**: A separate Docker daemon inside a Sysbox container.

## Security model

Mounts grant Claude read and write access to host files. Forwarded environment variables
can grant access to credentials and external services. Use narrow workspace
roots. Forward only the variable names that you need.

Process environments are not secret stores. Claude, its child processes,
the Docker daemon, and users who can inspect processes can read forwarded values.

Docker group membership is root-equivalent on the host. Containers share
the host kernel. They do not provide a boundary against a hostile host
administrator. This project never mounts the host Docker socket and does
not use privileged mode.

Desktop uses a private nested display and an Xauthority cookie. The
Chromium sandbox is enabled by default. Disabling it is unsafe. Clipboard
synchronization can transfer secrets between the host and Desktop.
Both unsafe options require an explicit request.

Sysbox provides a separate inner Docker daemon. On unsupported hosts,
including Debian 13, do not assume that Sysbox provides host isolation.
See the support limits below before you enable it.

## Requirements

For the normal CLI path, install these host prerequisites:

- Linux on amd64 (x86_64) or arm64 (aarch64).
- Python 3.11 or newer.
- Docker Engine with the Docker Compose v2 plugin.
- Access to the Docker daemon as your normal user.

Use your distribution or Docker vendor instructions to install prerequisites.
The launcher does not install host packages or change Docker settings.
Images use Ubuntu 26.04. Image tools need no separate host tool manager.

Ubuntu 26.04 CLI and Desktop image/tool checks passed on amd64. The Ubuntu 26.04.1 LTS
Desktop image also passed the Chromium-policy GUI test on Docker 29.10.0.

| Configuration | Intended reference | Actual qualification in this series |
| --- | --- | --- |
| CLI, no inner Docker, amd64 | Debian 13; Ubuntu 24.04 | Ubuntu 26.04 image and CLI image-runtime checks passed on the Debian 13 Docker host. Full interactive host-identity qualification remains open. |
| CLI, no inner Docker, arm64 | Debian 13; Ubuntu 24.04 | arm64 build and runtime not executed. |
| Desktop, amd64 | Linux graphical session with Xephyr | Historically, the Ubuntu 24.04 default-policy sandboxed Desktop FAILED on the Debian 13 Docker host: namespace EPERM, exit 133. Xauthority, nested clipboard, and cleanup checks passed. No unsafe fallback was used. |
| Desktop with explicit chromium policy, amd64 | Docker Engine Community 29.10.0, Debian 13 amd64, no inner Docker | The Ubuntu 26.04.1 LTS image passed the real host-identity launcher/Xephyr test described below. |
| Desktop, arm64 | Linux graphical session with Xephyr | Not executed. |
| Inner Docker with Sysbox | Ubuntu 24.04 | Live Sysbox and nested Docker not executed. Debian 13 is experimental. |
| Shared system install | Linux with administrator installation | Temporary simulated layouts tested; real privileged and cross-account use not executed. |

## Install

1. Obtain a trusted release archive or source checkout.
2. Open a terminal in its extracted directory.
3. Install for your user:

   ```bash
   ./install
   ```

4. If `$HOME/.local/bin` is absent from `PATH`, add it in your shell startup file:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

5. Create the private launcher configuration:

   ```bash
   claude-docker config init
   ```

6. Open `~/.config/claude-docker/config.toml` in your editor.
7. Replace the example mount list with existing workspace roots. For example,
   set `mounts = ["~/work/git"]` in the existing `[container]` table.
8. To add optional tools and apt packages, create a user tool profile:

   ```bash
   claude-docker tools init
   ```

   Review or edit `~/.config/claude-docker/mise.toml`, then lock its tools:

   ```bash
   claude-docker tools lock
   ```

9. If you want the personal build hook, create its example:

   ```bash
   claude-docker customize init
   ```

   Review or edit `~/.config/claude-docker/customize.sh` before you build.
   The example installs `checkbashisms` and Google Cloud CLI as root.
   This script needs no `tools lock`.
10. Check the host and build the CLI image:

    ```bash
    claude-docker check
    claude-docker build
    ```

    Use `claude-docker build --all` to build both CLI and Desktop images.

Steps 8 and 9 are optional. Without these files, the build uses only the base
tools. Init commands do not overwrite existing files. Mise is not required
on the host.

The default installation stores versioned program files under
`~/.local/share/claude-docker/releases`. The command link is
`~/.local/bin/claude-docker`. Installation preserves existing configuration.
`config init` refuses to overwrite an existing file.

## Start Claude

Run `claude-docker` without arguments or with `--help` to show full help.
This does not read configuration or use Docker, and it creates no files. To show paths for a command, put the global `-d` or `--debug` option before it.
Add `-d` before `--help` or `--version` to also list paths. Normal starts
report versions without listing paths. Build-input paths include base
and personal mise TOML/lock files and customize scripts; missing files are marked.
These host files can differ from those used to build an existing container.
Diagnostics use stderr. Without `-d`, help and version output stays concise.

After you complete the installation and image build:

1. Open a terminal in a configured workspace:

   ```bash
   cd ~/work/git
   ```

2. Start Claude Code:

   ```bash
   claude-docker claude
   ```

All arguments after `claude` go to Claude Code unchanged. For example:

```bash
claude-docker claude --resume
claude-docker claude --help
```

The launcher mounts each workspace at the same absolute path in the
container. If the current directory is outside the mounted workspaces,
Claude starts in the configured container home.

To stop the containers and recorded display services, run:

```bash
claude-docker stop
```

This command removes the containers. It preserves Claude state in enabled host mounts
and data in named inner Docker volumes.

## Configure the program

See [config.example.toml](config.example.toml) for detailed configuration keys.
Precedence is process environment overrides, then TOML settings, then detected
host defaults. CLI identity overrides can change the container account,
UID, GID, and home. The root account is not supported.

Commands that read configuration print the selected path to standard error.
If the default file is absent, the message reports the use of defaults and
environment overrides. Configuration contents and credential values are not printed.

Without a configuration file, the workspace default is `~/work/git`.
The generated example shows several mount roots. To mount multiple
workspaces, set this table in your configuration:

```toml
[container]
mounts = ["~/work/git", "~/work/etc", "/srv/shared-projects"]
```

Paths must be absolute or start with `~/`. Each root must exist.
An empty mount list disables workspace mounts.

Default host storage uses these paths:

| Purpose | Default path |
| --- | --- |
| Launcher configuration | `~/.config/claude-docker/config.toml` |
| Program releases and installation records | `~/.local/share/claude-docker` |
| Launcher state | `~/.local/state/claude-docker` |
| Launcher cache | `~/.cache/claude-docker` |
| Private runtime | `$XDG_RUNTIME_DIR/claude-docker`, if the runtime base is private and usable; otherwise `/tmp/claude-docker-<uid>` |
| Claude Code state | `~/.claude` |
| Claude Code JSON state | `~/.claude.json` |
| Claude Desktop profile | `~/.config/Claude` |

Absolute XDG base-directory variables replace their corresponding defaults.
Claude state mounts are enabled by default. A missing Claude Code JSON file
is not created. You can change a state source or disable its mount.
If you disable a state mount, or if the host Claude Code JSON file does not exist,
Claude writes the affected data as container-local state. This state is lost when
the container is removed. A new container does not restore this state.

`CLAUDE_CONFIG_FILE` selects another TOML file. An explicitly selected file
must exist. `CLAUDE_MOUNTS_FILE` replaces the TOML workspace list with one
host path per line; blank lines and lines that start with `#` are ignored.
For advanced source layouts, `CLAUDE_DOCKER_DIR` selects an absolute program
directory that contains the launcher package.

First, export the AWS credential variables in your host shell.
Then add their names to your configuration:

```toml
[environment]
required = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
optional = ["AWS_REGION", "AWS_DEFAULT_REGION"]
```

Keep credential values in the host process environment. Required variables
must be present and nonempty. Optional variables are forwarded only when
present. The launcher does not save their values in TOML or generated files.

Each new CLI process receives the current selected host environment values.
An existing CLI process keeps its old values. After you refresh credentials,
start a new CLI process. For Desktop, restart its process:

```bash
claude-docker desktop --restart
```

Inner containers do not automatically inherit these variables. Pass each
required variable explicitly when you start an inner container.

### Optional shell aliases

After installation, add these lines to `~/.bashrc`:

```bash
alias claude='claude-docker claude'
alias claude-desktop='claude-docker desktop'
alias claude-stop='claude-docker stop'
alias claude-build='claude-docker build'
alias claude-rebuild='claude-docker rebuild'
```

Run `source ~/.bashrc` to apply them. For Zsh, use `~/.zshrc` instead.
The aliases pass arguments to the launcher. For example, use
`claude --resume`, `claude-desktop --restart`, or `claude-build --all`.
The `claude` alias takes precedence over a host command with the same name.

For a source checkout without installation, replace the launcher name in each
alias with the quoted absolute executable path. For example:

```bash
alias claude='"$HOME/work/claude-docker/claude-docker" claude'
```

Change the example path to your checkout path.

## Use Claude Desktop

Desktop requires a graphical host session with `DISPLAY` set. For a Wayland
session, XWayland must be available. Install Xephyr, xauth, and setxkbmap.
For clipboard transfers, also install xclip. On Debian or Ubuntu:

```bash
sudo apt install xserver-xephyr xauth x11-xkb-utils xclip
```

The container UID must match the host UID for private X11 access. Close any host
Claude app using the shared profile to avoid locks and concurrent writes.

1. Build the Desktop image:

   ```bash
   claude-docker build --desktop
   ```

2. Check the Desktop prerequisites:

   ```bash
   claude-docker check --desktop --clipboard
   ```

3. Start Desktop:

   ```bash
   claude-docker desktop
   ```

Resize the Xephyr window to resize Claude. Alt+Shift switches between US and
Russian inside Xephyr. Set `keyboard_layout` and `keyboard_options` in `[desktop]`
to use up to four comma-separated XKB layouts and comma-separated XKB options.
An empty option string clears the options. `CLAUDE_DESKTOP_KEYBOARD_LAYOUT` and
`CLAUDE_DESKTOP_KEYBOARD_OPTIONS` override TOML. Run `claude-docker desktop --restart`
to apply changes to a running session.

The default setting is `seccomp = "default"` in `[desktop]`. It preserves
Docker policy. To request the Chromium policy, set this in TOML:

```toml
[desktop]
seccomp = "chromium"
```

`CLAUDE_DESKTOP_SECCOMP` overrides TOML. Only `default` and `chromium` are
accepted. Chromium requires Linux amd64 and builtin seccomp. In both
`none` and `sysbox` modes, the launcher reads the outer daemon GitCommit.
First launch uses HTTPS to match that commit to an official Moby release tag and resolve its profile.
This traces source provenance and cannot detect unpublished vendor changes.
A matching verified cache works offline. A Docker update selects a new
cache entry automatically. Resolution errors stop launch with no automatic fallback.
Four exact kernel namespace operations expand the kernel attack surface. Keep the internal sandbox enabled. Use `claude-docker desktop --restart`
when the running profile differs. Restart clears stale Xephyr state and recreates only Desktop; it requires an unchanged Docker mode with no pending transition. Xephyr disables MIT-SHM to keep container IPC private. Live Sysbox remains unqualified.

See the [Desktop security notes](docs/desktop-security.md) for renderer results and why the GPU sandbox remains unqualified.

The default clipboard mode is manual. To copy the host clipboard into
Desktop, run:

```bash
claude-docker clipboard push
```

To copy the Desktop clipboard onto the host, run:

```bash
claude-docker clipboard pull
```

Review clipboard contents before a transfer; they can contain secrets.
`--clipboard-sync` explicitly enables background bidirectional transfers.
`--no-sandbox` explicitly disables the Chromium sandbox and is unsafe.
Do not use it to turn the known sandbox failure into a qualification pass.
The same risks apply to the corresponding configuration settings.

Per-run sandbox and clipboard choices persist until restart or stop.
Changing configuration alone does not reset a running Desktop process.
To apply current configuration and refresh credentials, run:

```bash
claude-docker desktop --restart
```

## Use inner Docker

To build or run containers from Claude, install Sysbox and register the
`sysbox-runc` runtime with Docker. Ubuntu 24.04 is the intended reference host.

Debian 13 Sysbox use is experimental. Filesystem hangs can make the host
unresponsive. Live Sysbox and nested Docker qualification remains open.

1. Stop the current containers:

   ```bash
   claude-docker stop
   ```

2. Set the top-level mode in your TOML configuration:

   ```toml
   docker_mode = "sysbox"
   ```

3. Check the host prerequisites:

   ```bash
   claude-docker check
   ```

4. Pull the external images for this mode:

   ```bash
   claude-docker pull
   ```

5. Start a new CLI process:

   ```bash
   claude-docker claude
   ```

On Desktop launch, restart, or reuse, the launcher creates or starts inner Docker
as needed and waits up to 120 seconds for health before changing the Desktop
session. Dependency repair keeps existing containers. If the image is missing, run `claude-docker pull` before retrying.

The inner daemon uses mutual TLS and a managed data volume. To switch back
to the default mode, stop the containers and set `docker_mode = "none"`.

If you intend to permanently delete the managed inner Docker data, stop
its containers first. Then request the volume inventory and confirmation:

```bash
claude-docker cleanup inner-docker
```

Use `--yes` for noninteractive deletion. Cleanup verifies volume names and ownership labels.

## Check the system

Run the read-only prerequisite checks:

```bash
claude-docker check
```

Use `--desktop` for Desktop prerequisites. Add `--clipboard` for manual
clipboard tools, or `--clipboard-sync` to check the synchronization policy.
The output names missing environment variables without printing values.

Desktop checks describe the requested policy for the next start. They do
not verify a running Desktop lifetime. Existing per-run choices and
credentials remain in that process until restart or stop.

If you intend to create and remove a temporary smoke container, run:

```bash
claude-docker check --smoke
```

This checks basic container execution. It does not qualify the Chromium
sandbox, interactive Desktop, or a complete nested Docker workload.

## Update or uninstall

Run `./install` from a newer trusted release to update the launcher.
For changed sources with the same version, run `claude-docker uninstall`
without `--purge`, then run `./install`. The installer verifies immutable
bundles. Both workflows retain user configuration, state, and build inputs.

### Update Claude Code or Desktop

```bash
claude-docker build --update-claude
claude-docker build --update-desktop
claude-docker build --all --update-claude --update-desktop
```

`build` defaults to CLI only. `--desktop` or `--update-desktop` selects Desktop,
and `--all` builds both. The first build pins the latest release for each
selected image. The update flags refresh their own pins, and Code keeps a newer
saved version. Both pins live in `~/.config/claude-docker/` as `claude-code.version`
and `claude-desktop.version`. `XDG_CONFIG_HOME` changes the base directory.
Install, uninstall, and purge retain both pins. Saved pins skip version lookups.

Both lookups show progress on stderr and keep the old pin on failure.
Code reads official HTTPS release metadata using Python and requires HTTP 200.
The lookup has a 30-second limit and needs no Docker or extra host tools.
Desktop fetches fresh signed Anthropic apt metadata without Ubuntu package
indexes. The selected Docker daemon caches a small Ubuntu helper for its native
architecture. Preparing it can pull the Ubuntu and mise images. The daemon
rebuilds it when its inputs change or it is removed. Desktop has a five-minute
lookup limit plus ten seconds for cleanup. Its helper survives uninstall and
purge. Image downloads have separate transfer limits.
To retry a build with saved versions, keep the target and omit the update flags.

A version-only `build` can reuse system packages, tools, and customization
layers. `claude-docker rebuild` uses the same flags with fresh base images
and no cache. A raw Docker build uses the shipped `latest` policies when the
artifact layers run. Code updates inside the containers are disabled.

Run `claude-docker stop` before starting Code from the new image.
Run `claude-docker desktop --restart` to use a rebuilt Desktop image with
your existing profile.

### Add image tools and packages

Installed commands support optional user tools. Create a private example once:

```bash
claude-docker tools init
```

Edit `~/.config/claude-docker/mise.toml`. The example includes kubectl 1.36,
AWS CLI v2, Helm, Terraform, and Ansible Core. Remove entries you do not need.
Its optional apt packages are build-essential (includes make), wget,
openssh-client, rsync, postgresql-client, mysql-client, and redis-tools.
Mise Python 3.14 supports locked Ansible. uv stays in the baseline.
Example profile:

```toml
[tools]
kubectl = "1.36"
aws-cli = "2"
helm = "latest"
terraform = "latest"
python = "3.14"
"pipx:ansible-core" = "latest"

[bootstrap.packages]
"apt:build-essential" = "latest"
"apt:wget" = "latest"
"apt:openssh-client" = "latest"
"apt:rsync" = "latest"
"apt:postgresql-client" = "latest"
"apt:mysql-client" = "latest"
"apt:redis-tools" = "latest"
# "apt:tree" = "latest"
```

The lock selects an exact patch version for each version request.

Lock artifacts, build both images, remove old containers, then launch:

```bash
claude-docker tools lock
claude-docker build --all
claude-docker stop
claude-docker claude
```

Use `build` for CLI or `build --desktop` for Desktop. Tools are installed in
system paths outside mounted HOME. Manual installations are lost when containers
are removed. Both images include these default tools:

| Installer | Tools |
| --- | --- |
| apt | ripgrep, jq, shellcheck, shfmt, sd, miller, hyperfine, bats, less, bsdextrautils |
| mise | fd, bat, ast-grep, yq (Mike Farah), gh, uv, difftastic, scc, duckdb |

Miller provides `mlr`. Difftastic provides `difft`. Kubectl remains optional.

The sibling `mise.lock` pins artifacts and checksums for `linux-x64` and
`linux-arm64`. Python tools use `pipx:` and retain their dependency graphs
under `.mise/locks/claude-docker/` beside the profile. Keep this directory with
the profile and its sibling lock; builds verify its digests. The first Python lock
prepares a cached helper with Python and uv. The host needs neither tool.
Ordinary locking preserves matching resolutions. Use
`claude-docker tools lock --bump` to advance requests. Builds require a matching
lock and never create or refresh one. Comments and apt edits keep tool locks
valid. An apt-only profile needs no artifact lock; apt packages are not pinned
by this lock. The shipped base and user artifact locks are separate.

`XDG_CONFIG_HOME` changes the default directory. `CLAUDE_MISE_CONFIG_FILE`
selects an absolute or `~/` file, independently of `CLAUDE_CONFIG_FILE`.
For example, `~/tools/custom.toml` uses the sibling `~/tools/custom.lock`.
A missing default profile adds no tools. An explicitly selected missing file
is an error. Init never overwrites a file. The profile, lock,
and Python lock directories survive updates, uninstall, and purge, including custom locations.

Only `[tools]` and apt-only `[bootstrap.packages]` are supported. Use a single
version string or a version/options table with scalar backend options.
Requests must be additive. Baseline removal, other sections, unsupported
managers, templates, core controls, and non-Python dependency sidecars are rejected.
Checksum and locked-install policy stay program-controlled. Docker receives
private snapshots of exact build inputs, without sending your HOME,
configuration directory, or workspace.

Manually migrate old edits from `image/mise.toml` into your user profile, then
lock and rebuild. Updates do not copy those edits or overwrite user profiles.
Contributors can edit the baseline hook `image/customize.sh` and run
`./claude-docker tools lock --base` in a trusted source or development checkout.
Use `./install --dev` to link a development checkout.

### Activate mise in Claude Code

Merge this entry into `~/.claude/settings.json`. Keep existing settings and hooks.
The default Claude state mount makes this host file available in the container.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "[ -n \"$CLAUDE_ENV_FILE\" ] && echo 'eval \"$(mise hook-env -s bash)\"' >> \"$CLAUDE_ENV_FILE\"; true"
          }
        ]
      }
    ]
  }
}
```

If Claude Code runs commands in zsh, replace `-s bash` with `-s zsh`.
Start a new Claude Code session after the change. Mise runs inside the container;
it is not required on the host. See the [mise integration instructions](https://mise.jdx.dev/agents.html#claude-code).

### Tell Claude Code which tools to use

You can append this section to the local `~/.claude/CLAUDE.md` file.
Keep your existing instructions. The default Claude state mount exposes this
file inside the container.

```markdown
## System tools

- Search: use `rg` for content and `fd` for file names. Limit output with
  `rg -l`, `rg -m 5`, and `--max-columns 200`.
- Replace text: use `sd` for simple replacements and `ast-grep` for structural
  code changes. Do not use `sed -i`.
- JSON: use `jq`. YAML, TOML, and XML: use `yq` (the Mike Farah Go version,
  not the Python wrapper).
- Tables and logs: use `mlr` for simple transformations. Use
  `duckdb -c "SELECT ... FROM 'file.csv'"` for aggregation and Parquet.
- Python: use `uv` instead of pip and venv. GitHub: use `gh`.
- Repository overview: use `scc`. Code diffs: use `difft`.
- Bash scripts: run `shellcheck` before you finish. For scripts that must work
  with POSIX sh, check portability with `dash script.sh`. Write tests with
  `bats`. To debug, use `PS4='+ ${BASH_SOURCE}:${LINENO}: ' bash -x script.sh`.
```

### Run a personal build script

Create a private example for `checkbashisms` and Google Cloud CLI:

```bash
claude-docker customize init
```

Edit `~/.config/claude-docker/customize.sh`. `XDG_CONFIG_HOME` changes the
default directory. The script runs as root after tool setup in each image
build, before the Claude artifacts are installed. The example installs to
`/usr/local/bin`, outside mounted HOME. Google Cloud CLI uses the official
`packages.cloud.google.com` apt repository with a scoped signing key.
Remove example sections you do not need. Init does not change existing scripts.
Run `claude-docker build --all`, run `claude-docker stop`, then start Claude.
Artifact locks apply to the mise profile. Keep secrets out of your script.
Docker receives its contents. Environment forwarding applies when Claude starts.
To disable the script, rename it and build again. Updates, uninstall, and
purge retain the script.

For implementation details, see [architecture](docs/architecture.md) and
[Desktop security](docs/desktop-security.md).

### Uninstall

For normal removal, run:

```bash
claude-docker uninstall
```

Normal uninstall removes owned program files and managed containers.
It retains launcher configuration, user data, images, networks, and volumes.

To also purge eligible launcher configuration and managed Docker storage,
request an inventory and give interactive consent:

```bash
claude-docker uninstall --purge
```

For noninteractive purge, explicitly confirm with `--yes`:

```bash
claude-docker uninstall --purge --yes
```

Purge checks exact ownership labels: the prefix is
`io.github.diborodin.claude-in-docker-linux.`, with `managed=true` and
`owner-uid` equal to your host UID. It does not run a global Docker prune.
All removal modes retain `~/.claude`, `~/.claude.json`, `~/.config/Claude`,
custom state sources, and workspaces. Protected paths take priority over
program ownership, even during purge.

For a shared installation, an administrator installs with
`sudo ./install --system`. Each user still has separate launcher configuration
and state. To remove the shared program, use trusted matching source:

```bash
sudo ./install --uninstall --system
```

If removal is interrupted, the installed command can already be absent.
From trusted matching source, use the same HOME and XDG environment as the
original user installation and resume removal:

```bash
./install --uninstall
```

A pending purge retains its original purge choice and protected inventory.
If Docker cleanup remains pending, retry requires the usual consent.
Do not edit or delete private removal records. Finish removal before you
reinstall. If another install or uninstall is busy, wait for it to finish
before you retry.

## TODO

The following work is not complete. Items are listed in the proposed order.

- [ ] Add CI for unit tests, image builds, and release archive checksums.
- [ ] Complete interactive CLI checks on Debian 13 and Ubuntu 24.04.
  Build and test the arm64 images.
- [ ] Extend Desktop checks to login, session resume, real profiles, host
  clipboard transfers, and the internal GPU sandbox. Review other Docker versions.
- [ ] Test Sysbox and inner Docker on Ubuntu 24.04. Investigate Debian 13 hangs.
  Qualify Desktop with the Chromium policy in Sysbox mode separately.
- [ ] Test shared system installation and removal with separate real accounts.

## License

Copyright (C) 2026 Claude in Docker for Linux contributors.

The original project code and documentation use GNU GPL version 3 only
(`GPL-3.0-only`). See [LICENSE](LICENSE). The program comes without warranty.
Moby seccomp profiles use Apache 2.0. See the [license](security/LICENSE.moby-profiles) and [notice](security/NOTICE.moby-profiles).
Claude and other third-party software retain their own licenses.
