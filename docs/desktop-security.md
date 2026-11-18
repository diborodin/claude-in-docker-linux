# Desktop security

Use the [user manual](../README.md#use-claude-desktop) for setup and commands.
This document describes the policy and the limits of current test evidence.

## Display, sandbox, and clipboard

Desktop runs in a private Xephyr display with an Xauthority cookie. The display
is authenticated; Xephyr is not started with `-ac`. The container UID must match
the host UID. Xephyr disables MIT-SHM because the container uses private IPC.
Pixel transfers can require extra copying. Close a host Desktop process before sharing its profile directory.

The internal Chromium sandbox is enabled by default. Clipboard transfer is
manual by default. `--no-sandbox` and `--clipboard-sync` require explicit opt-in.
Disabling the sandbox removes an application isolation layer. Clipboard sync can
transfer secrets in either direction. A configuration change does not change an
existing process; restart Desktop to apply it.

## Seccomp selection

`[desktop].seccomp` accepts `default` or `chromium`.
`CLAUDE_DESKTOP_SECCOMP` overrides the TOML setting.

`default` preserves Docker policy. The explicit `chromium` setting applies to
Desktop in either Docker mode and requires the internal sandbox. The launcher
queries the outer Linux amd64 daemon for its Engine version, GitCommit and
builtin seccomp status. Custom, disabled and untraceable defaults fail closed.

## Policy provenance and change

On first use the launcher follows the daemon GitCommit to official Moby
source over HTTPS, resolves its profiles dependency and fetches the baseline
and license at the resulting source revision. This traces reported source
history by requiring an exact commit match to the published Moby
`docker-v<version>` or `v<version>` tag. Annotated tags are dereferenced, including
the published profiles module tag. A vendor can ship unpublished binary changes under the same metadata.

Older entries without release-ref proof require fresh HTTPS resolution. Use a real
private cache directory beneath trusted ancestors without symlinks. The ten-second
network timeout applies per blocking operation. The private cache records source and profile commits and verifies the derived
policy on each reuse, including offline launches. A Docker update selects
a matching cache entry or resolves a new one. Read-only Desktop checks warn
on a cache miss. Restart Desktop to apply a changed policy. Stop and recovery
operate independently of cache health. See the [Moby notice](../security/NOTICE.moby-profiles).

The matching Docker baseline is preserved. Exactly four allow rules are added.
Each rule compares syscall argument 0 with `SCMP_CMP_EQ` on the x86_64 ABI:

| Syscall | Flags | Decimal value |
| --- | --- | --- |
| `clone` | `CLONE_NEWUSER` + `SIGCHLD` | 268435473 |
| `unshare` | `CLONE_NEWUSER` | 268435456 |
| `clone` | `CLONE_NEWUSER` + `CLONE_NEWPID` + `CLONE_NEWNET` + `SIGCHLD` | 1879048209 |
| `clone` | `CLONE_NEWPID` + `SIGCHLD` | 536870929 |

There are no other additions to the baseline. These operations expand the
kernel interface available to Desktop. The profile is not a universal security
upgrade. A conflicting upstream rule stops profile generation.

## Qualification evidence and limits

The historical Ubuntu 24.04 image investigation on Debian 13 amd64 found that default-policy sandboxed Desktop
failed with namespace `EPERM` and exit 133. No unsafe fallback was used.

The previous static policy on Docker Engine Community 29.9.2 passed a real host-identity launcher test with
authenticated Xephyr and isolated temporary Claude state and workspace. A Claude
window appeared. Three renderers had separate user/PID namespaces, zero
capabilities, `NoNewPrivs`, and two seccomp filters. The main and GPU processes
remained stable during a 20-second observation. Production stop removed the
managed containers, network, display, and cookie before fallback cleanup.

The unwrapped launcher passed on Debian 13 amd64 with Docker 29.10.0, the Ubuntu
26.04.1 LTS image, and no inner Docker. Cache resolution version 2 matched
`refs/tags/docker-v29.10.0` to daemon commit `06db173`. Docker applied the
Moby profiles v0.3.0 baseline plus four rules. Authenticated Xephyr displayed a
Claude window with MIT-SHM disabled. Three renderers passed the namespace,
capability, no-new-privileges, seccomp and AppArmor checks. The main and GPU
PIDs stayed stable for 20 seconds. Normal stop removed the container, network,
display and cookie before fallback cleanup.

An additional internal GPU seccomp filter was not established. This result does
not qualify hardware acceleration, the full GPU sandbox, login, real profiles, session resume, host
clipboard use, other Docker versions, arm64, or live Sysbox. A matching daemon
version alone does not prove that every host kernel or LSM permits startup.

## Reproduce the opt-in test

Use a prepared graphical Linux host with Docker access. In addition to the user
prerequisites, the test needs `xdpyinfo` and `xwininfo`. The test builds an image,
creates isolated resources, starts a real graphical application, and removes its
test resources. Do not treat it as a read-only check.

```bash
CLAUDE_DOCKER_DESKTOP_TEST=1 \
CLAUDE_DOCKER_CHROMIUM_SECCOMP_TEST=1 \
python3 -m unittest -v \
  tests.test_desktop_integration.DesktopIntegrationTests.test_chromium_seccomp_desktop
```

The test keeps its JSON report in a new `claude-desktop-artifacts-*` directory
under the system temporary directory and prints that path. Set
`CLAUDE_DOCKER_TEST_ARTIFACTS` to choose a report directory. The result file is
`desktop-seccomp-result.json`. Reports include local runtime metadata; review
them before sharing. The test captures detached application output and exit status
with a test-only shell wrapper. Set `CLAUDE_DOCKER_TEST_CAPTURE_APP=0` to
qualify the unwrapped command with profile log and OS-release capture. Remove retained reports when they are no longer needed.
