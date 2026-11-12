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
Image tools run inside Docker; they need no separate host tool manager.

The matrix separates intended reference configurations from actual
qualification evidence. It is not a full Desktop production qualification.

| Configuration | Intended reference | Actual qualification in this series |
| --- | --- | --- |
| CLI, no inner Docker, amd64 | Debian 13; Ubuntu 24.04 | Image and CLI image-runtime checks passed on the Debian 13 Docker host. Full interactive host-identity qualification remains open. |
| CLI, no inner Docker, arm64 | Debian 13; Ubuntu 24.04 | arm64 build and runtime not executed. |
| Desktop, amd64 | Linux graphical session with Xephyr | The default-policy sandboxed Desktop FAILED on the Debian 13 Docker host: namespace EPERM, exit 133. Xauthority, nested clipboard, and cleanup checks passed. No unsafe fallback was used. |
| Desktop with explicit chromium policy, amd64 | Docker Engine Community 29.9.2, Linux amd64, no inner Docker | Real host-identity launcher/Xephyr qualification is described below. Default-policy failure remains separate. |
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
7. Remove example mount roots that you do not need or that do not exist.

The default installation stores versioned program files under
`~/.local/share/claude-docker/releases`. The command link is
`~/.local/bin/claude-docker`. Installation preserves existing configuration.
`config init` refuses to overwrite an existing file.

## Start Claude

1. Build the CLI image:

   ```bash
   claude-docker build
   ```

2. Open a terminal in a configured workspace:

   ```bash
   cd ~/work/git
   ```

3. Start Claude Code:

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

The configured container UID must match the host UID for private X11 access.
CLI identity overrides with a different UID cannot be used for Desktop.
If the host Desktop app uses the shared Claude profile, close it first.
A shared Desktop profile can cause profile locking and concurrent-write errors.

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

The default setting is `seccomp = "default"` in `[desktop]`. It preserves
Docker policy. To request the bundled Chromium policy, set this in TOML:

```toml
[desktop]
seccomp = "chromium"
```

`CLAUDE_DESKTOP_SECCOMP` overrides TOML. Only `default` and `chromium` are
accepted. The chromium option supports only a Linux amd64 (x86_64) daemon
running Docker Engine Community 29.9.2 with builtin seccomp enabled and
`docker_mode = "none"`. Sysbox and unknown daemon versions are rejected.
This policy permits four exact kernel namespace creation operations and
expands the kernel attack surface. It cannot be combined with disabling
the internal sandbox. There is no automatic fallback if startup fails.
Use `claude-docker desktop --restart` to apply a changed setting.

The opted-in real launcher qualification observed authenticated Xephyr,
a Claude window, renderer user/PID namespaces, zero capabilities,
NoNewPrivs and two seccomp filters, and a GPU PID stable for 20 seconds.
Renderer evidence does not certify the internal GPU sandbox: an additional
GPU filter was not established. Login, real profiles, host clipboard,
other daemon versions, arm64 and live Sysbox remain unqualified.

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

Enable inner Docker only if you need to build or run containers from Claude.
Install Sysbox separately and register the `sysbox-runc` runtime with Docker.
Ubuntu 24.04 is the intended reference host for this mode.

Debian 13 Sysbox use is experimental. Known Sysbox filesystem hangs can
make the host unresponsive. Live Sysbox was not tested in this series.
Do not use a successful CLI check as evidence of nested Docker qualification.

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

The inner daemon uses mutual TLS and a managed data volume. It does not
connect to the host Docker socket. To return to the default mode, stop the
containers and set `docker_mode = "none"`.

If you intend to permanently delete the managed inner Docker data, stop
its containers first. Then request the volume inventory and confirmation:

```bash
claude-docker cleanup inner-docker
```

For noninteractive deletion, add `--yes`. Cleanup validates the exact volume
name and ownership labels before deletion.

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

To update, obtain a newer trusted release. From its extracted directory,
run `./install` again. The installer verifies owned files before replacement.
It retains configuration and user data. Do not edit installed bundles;
they are immutable.

To refresh the CLI image with current base images and no build cache, run:

```bash
claude-docker rebuild
```

Use `--desktop` for the Desktop image or `--all` for both images.
After a build or rebuild, stop existing containers before the next start:

```bash
claude-docker stop
```

### Add image packages

Use a trusted source or development checkout. Do not edit an installed bundle.
For example, to add `bsdextrautils`, add this entry to the existing
`[bootstrap.packages]` table in `image/mise.toml`. Keep the other entries:

```toml
[bootstrap.packages]
"apt:bsdextrautils" = "latest"
```

From that checkout, build both images and remove the old containers:

```bash
./claude-docker build --all
./claude-docker stop
```

Start a new process with `./claude-docker claude` or `./claude-docker desktop`.
Use `build` for only CLI, or `build --desktop` for only Desktop.
The package is available in each rebuilt image. A package installed manually
in a running container is lost when that container is removed.

Contributors can use `image/customize.sh` for advanced image changes.
For tools in the `[tools]` table, refresh the pinned tool lock for both
architectures from the checkout:

```bash
./claude-docker tools lock
```

This contributor command runs the pinned tool manager through Docker.
It is refused in installed bundles. For a development link, use
`./install --dev` from a trusted source checkout.

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

- [ ] Separate the base image tools from user tools. Keep `image/mise.toml` as
  the base. Add an optional `~/.config/claude-docker/mise.toml` for user packages
  and tools. Define override rules, separate lock files, and reproducible build
  inputs. Support installed releases without editing their immutable bundles.
- [ ] Pin Claude Code and Desktop versions and document how to update them.
- [ ] Add CI for unit tests, image builds, and release archive checksums.
- [ ] Complete interactive CLI checks on Debian 13 and Ubuntu 24.04.
  Build and test the arm64 images.
- [ ] Extend Desktop checks to login, session resume, real profiles, host
  clipboard transfers, and the internal GPU sandbox. Review other Docker versions.
- [ ] Test Sysbox and inner Docker on Ubuntu 24.04. Investigate Debian 13 hangs.
  Test Desktop with Sysbox separately before allowing the Chromium policy there.
- [ ] Test shared system installation and removal with separate real accounts.

## License

Copyright (C) 2026 Claude in Docker for Linux contributors.

The original project code and documentation use GNU GPL version 3 only
(`GPL-3.0-only`). See [LICENSE](LICENSE). The program comes without warranty.
The bundled Moby seccomp profile retains Apache 2.0; see its
[license](security/LICENSE.moby-profiles) and [notice](security/NOTICE.moby-profiles).
Claude and other third-party software retain their own licenses.
