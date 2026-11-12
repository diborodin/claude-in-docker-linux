# Desktop security

Use the [user manual](../README.md#use-claude-desktop) for setup and commands.
This document describes the policy and the limits of current test evidence.

## Display, sandbox, and clipboard

Desktop runs in a private Xephyr display with an Xauthority cookie. The display
is authenticated; Xephyr is not started with `-ac`. The container UID must match
the host UID. Close a host Desktop process before sharing its profile directory.

The internal Chromium sandbox is enabled by default. Clipboard transfer is
manual by default. `--no-sandbox` and `--clipboard-sync` require explicit opt-in.
Disabling the sandbox removes an application isolation layer. Clipboard sync can
transfer secrets in either direction. A configuration change does not change an
existing process; restart Desktop to apply it.

## Seccomp selection

`[desktop].seccomp` accepts `default` or `chromium`.
`CLAUDE_DESKTOP_SECCOMP` overrides the TOML setting.

`default` preserves Docker policy. `chromium` selects the bundled derived profile
for Desktop only. It requires all of these selected-daemon properties:

- Docker Engine Community 29.9.2.
- Linux, amd64 or x86_64.
- Builtin seccomp enabled.
- `docker_mode = "none"`.
- The internal Chromium sandbox enabled.

Unknown versions, other architectures, Sysbox, and sandbox-disable combinations
are rejected. Integrity and daemon checks run before startup creates resources.
The launcher does not download a policy or fall back to weaker settings.

CLI, builds, and default-policy Desktop do not acquire this profile. Read-only
Desktop checks describe the next requested start, not an existing process.
Stop and recovery do not require a successful compatibility check. Installed
bundle integrity rules still apply during uninstall.

## Policy provenance and change

The asset is
[`desktop-seccomp-v0.2.4-linux-amd64.json`](../security/desktop-seccomp-v0.2.4-linux-amd64.json).
It derives from Moby profiles tag `seccomp/v0.2.4`. Exact source URLs, hashes,
serialization, and attribution are in
[`NOTICE.moby-profiles`](../security/NOTICE.moby-profiles).
The upstream [Apache 2.0 license](../security/LICENSE.moby-profiles) is bundled.

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
upgrade. It can also omit protections introduced in later Docker policies.
Widen the compatibility allowlist only after comparing the matching upstream
policy and qualifying the new combination. Keep asset hashes, provenance,
installer inventory, and policy tests consistent.

## Qualification evidence and limits

The Debian 13 amd64 investigation found that default-policy sandboxed Desktop
failed with namespace `EPERM` and exit 133. No unsafe fallback was used.

The explicit Chromium policy passed a real host-identity launcher test with
authenticated Xephyr and isolated temporary Claude state and workspace. A Claude
window appeared. Three renderers had separate user/PID namespaces, zero
capabilities, `NoNewPrivs`, and two seccomp filters. The main and GPU processes
remained stable during a 20-second observation. Production stop removed the
managed containers, network, display, and cookie before fallback cleanup.

An additional internal GPU seccomp filter was not established. This result does
not qualify the full GPU sandbox, login, real profiles, session resume, host
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
them before sharing. Remove retained reports when they are no longer needed.
