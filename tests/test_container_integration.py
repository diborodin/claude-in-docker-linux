"""Opt-in real Docker image and Sysbox smoke tests."""

from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import pwd
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from tests import isolate_runtime_lock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_docker import cli
from claude_docker.commands import CommandRunner
from claude_docker.compose import ComposeProject
from claude_docker.config import AccountInfo, load_config
from claude_docker.paths import HostPaths


IDENTITY = {
    "CLAUDE_USER": "tester",
    "CLAUDE_UID": "10000",
    "CLAUDE_GID": "10000",
    "CLAUDE_HOME": "/home/tester",
    "CLAUDE_HOST_UID": str(os.getuid()),
}


def require_integration(environment, *, sysbox=False):
    """Require explicit opt-in before any Docker operation."""
    if environment.get("CLAUDE_DOCKER_INTEGRATION") != "1":
        raise unittest.SkipTest("set CLAUDE_DOCKER_INTEGRATION=1 to build and run Docker images")
    if sysbox and environment.get("CLAUDE_DOCKER_SYSBOX_TEST") != "1":
        raise unittest.SkipTest(
            "set CLAUDE_DOCKER_SYSBOX_TEST=1 as well, only on an explicitly prepared Sysbox host"
        )


class ContainerIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Gate before even probing Docker: a selected Sysbox test needs both flags.
        require_integration(os.environ, sysbox=self._testMethodName == "test_sysbox_inner_docker")
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.skipTest(f"Docker is not reachable: {error}")
        if result.returncode:
            self.skipTest(f"Docker is not reachable: {result.stderr.strip()}")
        self.docker_info = json.loads(result.stdout)
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            platform.machine(), platform.machine(),
        )
        self.build_platform = f"linux/{architecture}"
        self.images = {}
        self.token = f"{os.getuid()}-{os.getpid()}-{uuid.uuid4().hex[:10]}"

    def tearDown(self):
        # A timed-out docker client may leave a --rm container running.
        owned = self.command("docker", "ps", "-aq", "--filter",
                             f"label=claude-docker.integration={self.token}").splitlines()
        if owned:
            self.command("docker", "rm", "--force", *owned)
        self.assertEqual(self.command("docker", "ps", "-aq", "--filter",
                                      f"label=claude-docker.integration={self.token}"), "")
        # Registered before each build, so failed builds also lose only our tag.
        for image in self.images.values():
            found = self.command("docker", "image", "ls", "-q", "--filter", f"reference={image}")
            if found:
                self.command("docker", "image", "rm", image)
            self.assertEqual(self.command("docker", "image", "ls", "-q", "--filter",
                                          f"reference={image}"), "")

    def command(self, *arguments, timeout=120, env=None):
        if arguments[:2] == ("docker", "run"):
            arguments = (*arguments[:2], "--label", f"claude-docker.integration={self.token}",
                         *arguments[2:])
        result = subprocess.run(
            arguments, cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Command failed: {arguments!r}\n{result.stdout[-12000:]}\n{result.stderr[-12000:]}",
        )
        return result.stdout.strip()

    def build_image(self, target, *, identity=None, key=None):
        identity = IDENTITY if identity is None else identity
        key = target if key is None else key
        if key not in self.images:
            image = f"claude-docker-integration-{self.token}-{key}:test"
            self.images[key] = image
            arguments = ["docker", "build", "--platform", self.build_platform,
                         "--target", target, "--tag", image]
            for name, value in identity.items():
                arguments.extend(["--build-arg", f"{name}={value}"])
            print(f"Building {image} ({self.build_platform}); deadline 7200s", flush=True)
            build = subprocess.run([*arguments, "."], cwd=ROOT, timeout=7200, check=False)
            self.assertEqual(build.returncode, 0, f"Image build failed: {image}")
            print(f"\nBuilt {target}: {image} ({self.build_platform}, "
                  f"UID/GID {identity['CLAUDE_UID']}/{identity['CLAUDE_GID']})", flush=True)
        return self.images[key]

    def test_image_matrix(self):
        # Catch runtime drift, hidden system tools under a home mount, a root
        # final user, a missing Code executable, or GUI packages leaking to CLI.
        probes = {}
        for target in ("cli", "desktop"):
            with self.subTest(target=target):
                image = self.build_image(target)
                output = self.command(
                    'docker', 'run', '--rm', '--network', 'none', image, 'sh', '-ec',
                    'id -u; id -g; ! command -v kubectl; jq --version; less --version | head -1; '
                    'command -v column; mise --version; claude --version')
                lines = output.splitlines()
                self.assertEqual(lines[:2], ['10000', '10000'])
                self.assertRegex(lines[2], r'^jq-')
                self.assertIn('less', lines[3])
                self.assertEqual(lines[4], '/usr/bin/column')
                versions = lines[5:]
                self.assertEqual(len(versions), 2, output)
                self.assertRegex(versions[1], r'^\d+\.\d+\.\d+ \(Claude Code\)$')
                buildx = self.command('docker', 'run', '--rm', '--network', 'none', image,
                                      'docker', 'buildx', 'version')
                self.assertRegex(buildx, r'github\.com/docker/buildx v\d+\.\d+\.\d+')
                probes[target] = lines[2:6]
                packages = self.command(
                    "docker", "run", "--rm", "--network", "none", image,
                    "dpkg-query", "-W", "-f=${binary:Package} ${db:Status-Status}\\n",
                )
                installed = {line.split()[0].split(":")[0] for line in packages.splitlines()
                             if line.endswith(" installed")}
                self.assertIn("docker-ce-cli", installed, "package probe returned no installed Docker client")
                self.assertIn("docker-buildx-plugin", installed)
                self.assertTrue({'ripgrep', 'jq', 'shellcheck', 'shfmt', 'sd', 'miller',
                                 'hyperfine', 'bats', 'less', 'bsdextrautils'} <= installed)
                for mounts in ((), ('--tmpfs', '/home/tester:rw,uid=10000,gid=10000')):
                    self.command('docker', 'run', '--rm', '--network', 'none', *mounts, image,
                                 'sh', '-ec', 'fd --version; bat --version; ast-grep --version; '
                                 'yq --version; gh --version; uv --version; difft --version; '
                                 'scc --version; duckdb --version; rg --version; '
                                 'shellcheck --version; shfmt --version; sd --version; '
                                 'mlr --version; hyperfine --version; bats --version')
                gui = {name for name in installed if name in {"claude-desktop", "openbox", "libnss3"}
                       or name.startswith(("libgtk", "libx11", "fonts-"))}
                if target == "cli":
                    self.assertEqual(gui, set(), f"GUI packages leaked into CLI: {gui}")
                else:
                    self.assertTrue({"claude-desktop", "openbox"} <= gui, gui)
                mounted = self.command(
                    'docker', 'run', '--rm', '--network', 'none', '--tmpfs',
                    '/home/tester:rw,uid=10000,gid=10000', image, 'sh', '-ec',
                    '! command -v kubectl; jq --version; less --version | head -1; '
                    'command -v column; mise --version; test ! -e "$HOME/.local/share/mise/installs"')
                self.assertEqual(mounted.splitlines(), probes[target])
                mounted_claude = self.command(
                    "docker", "run", "--rm", "--network", "none", "--tmpfs",
                    "/home/tester:rw,uid=10000,gid=10000", image, "sh", "-ec",
                    'command -v claude; test ! -L /usr/local/bin/claude; '
                    'test ! -w /usr/local/bin/claude; test "$DISABLE_UPDATES" = 1; '
                    'test "$(stat -c "%u:%g:%a" /usr/local/bin/claude)" = 0:0:555; claude --version; '
                    'mkdir -p "$HOME/.local/bin"; '
                    'printf "#!/bin/sh\\nexit 99\\n" > "$HOME/.local/bin/claude"; '
                    'chmod 0755 "$HOME/.local/bin/claude"; '
                    'command -v claude; claude --version',
                )
                self.assertEqual(mounted_claude.splitlines(),
                                 ["/usr/local/bin/claude", versions[1]] * 2)
                print(f"{target}: baseline tools verified, mise={versions[0]}, Code={versions[1]}",
                      flush=True)
                print(f"{target}: {buildx}; Code survives empty and shadowing home mounts", flush=True)
        self.assertEqual(probes["cli"], probes["desktop"])

    def test_custom_cli_tools_survive_empty_home_and_stable_lock(self):
        from claude_docker.image_tools import image_build_inputs, lock_image_tools
        from claude_docker.paths import resolve_host_paths
        with tempfile.TemporaryDirectory(prefix='claude-custom-smoke-') as temporary:
            home = Path(temporary)
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'))
            env.pop('CLAUDE_MISE_CONFIG_FILE', None)
            paths = resolve_host_paths(env, os.getuid(), create_runtime=False)
            paths.config_dir.mkdir(parents=True)
            source = paths.config_dir / 'mise.toml'
            raw = b'[tools]\nkubectl="latest"\n[bootstrap.packages]\n"apt:tree"="latest"\n'
            source.write_bytes(raw)
            runner = CommandRunner()
            lock_image_tools(ROOT, paths, runner, env)
            locked = source.with_suffix('.lock').read_bytes()
            lock_image_tools(ROOT, paths, runner, env)
            self.assertEqual(source.with_suffix('.lock').read_bytes(), locked)
            image = f'claude-docker-integration-{self.token}-custom:test'
            self.images['custom'] = image
            with image_build_inputs(ROOT, paths, env) as context:
                args = ['docker', 'build', '--platform', self.build_platform, '--target', 'cli', '--tag', image]
                for key, value in IDENTITY.items():
                    args.extend(['--build-arg', f'{key}={value}'])
                self.command(*args, str(context), timeout=7200)
                self.command(*args, str(context), timeout=7200)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(source.with_suffix('.lock').read_bytes(), locked)
            expected = tomllib.loads(locked.decode())['tools']['kubectl'][0]['version']
            for mounts in ((), ('--tmpfs', '/home/tester:rw,uid=10000,gid=10000')):
                output = self.command('docker', 'run', '--rm', '--network', 'none', *mounts, image,
                    'sh', '-ec', 'id -u; id -g; command -v kubectl; mise which kubectl; '
                    'kubectl version --client -o json; rg --version | head -1; jq --version; '
                    'command -v column; tree --version | head -1; test ! -e "$HOME/.local/share/mise/installs"')
                lines = output.splitlines()
                self.assertEqual(lines[:3], ['10000', '10000', '/usr/local/share/mise/shims/kubectl'])
                self.assertTrue(lines[3].startswith('/usr/local/share/mise/installs/kubectl/'))
                version, _ = json.JSONDecoder().raw_decode('\n'.join(lines[4:]))
                self.assertEqual(version['clientVersion']['gitVersion'], 'v' + expected)
                self.assertIn('ripgrep ', output)
                self.assertIn('tree v', output)

    def remove_cli_project(self, project_name):
        # Independent of launcher/Compose teardown; only our exact random project.
        for query, remove in ((["docker", "ps", "-aq"], ["docker", "rm", "--force"]),
                              (["docker", "network", "ls", "-q"], ["docker", "network", "rm"])):
            owned = self.command(*query, "--filter",
                                 f"label=com.docker.compose.project={project_name}",
                                 timeout=20).splitlines()
            if owned:
                self.command(*remove, *owned, timeout=60)

    def test_ordinary_cli_launcher_stop_failure_cleans_resources(self):
        self.check_cli_stop_boundary(23, "23 != 0")

    def test_ordinary_cli_launcher_noop_stop_is_rejected(self):
        self.check_cli_stop_boundary(0, "Launcher stop left project resources")

    def check_cli_stop_boundary(self, stop_result, expected_failure):
        # Only stop is replaced; real launch and fixture cleanup remain active.
        project_name = "claude-cli-smoke-" + self.token
        real_main = cli.main

        def failed_stop(argv):
            return stop_result if argv == ["stop"] else real_main(argv)

        try:
            with patch.object(cli, "main", side_effect=failed_stop):
                with self.assertRaisesRegex(AssertionError, expected_failure):
                    self.test_ordinary_cli_launcher()
            for query in (["docker", "ps", "-aq"], ["docker", "network", "ls", "-q"]):
                self.assertEqual(self.command(*query, "--filter",
                                 f"label=com.docker.compose.project={project_name}"), "")
        finally:
            # Keep this regression test safe even against the broken cleanup.
            self.remove_cli_project(project_name)

    def test_ordinary_cli_launcher(self):
        # Catch identity, path, environment or mode drift through the real CLI.
        account = pwd.getpwuid(os.getuid())
        identity = dict(IDENTITY, CLAUDE_USER=account.pw_name,
                        CLAUDE_UID=str(os.getuid()), CLAUDE_GID=str(os.getgid()),
                        CLAUDE_HOME=account.pw_dir)
        image = self.build_image("cli", identity=identity, key="host-cli")
        project_name = "claude-cli-smoke-" + self.token
        environment = {key: value for key, value in os.environ.items()
                       if key in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                                  "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "SSH_AUTH_SOCK",
                                  "SSL_CERT_FILE", "SSL_CERT_DIR")}
        with tempfile.TemporaryDirectory(prefix="claude-cli-smoke-") as temporary:
            root = Path(temporary)
            isolate_runtime_lock(self, root)
            for name in ("config", "data", "state", "cache", "runtime", "workspace", "code"):
                (root / name).mkdir(mode=0o700)
            workspace = root / "workspace"
            (workspace / "marker").write_text("isolated workspace\n")
            (root / "code.json").write_text("{}\n")
            config = root / "config.toml"
            # Preserve the actual host HOME default; all state paths are temporary.
            config.write_text(f'''docker_mode = "none"
[container]
mounts = [{json.dumps(str(workspace))}]
[state.code]
source = {json.dumps(str(root / "code"))}
[state.code_json]
source = {json.dumps(str(root / "code.json"))}
[state.desktop]
mount = false
[environment]
required = ["RELEASE_SMOKE_REQUIRED"]
optional = ["RELEASE_SMOKE_EMPTY", "RELEASE_SMOKE_ABSENT"]
''')
            environment.update(HOME=account.pw_dir, XDG_CONFIG_HOME=str(root / "config"),
                               XDG_DATA_HOME=str(root / "data"), XDG_STATE_HOME=str(root / "state"),
                               XDG_CACHE_HOME=str(root / "cache"), XDG_RUNTIME_DIR=str(root / "runtime"),
                               CLAUDE_CONFIG_FILE=str(config), CLAUDE_DOCKER_DIR=str(ROOT),
                               RELEASE_SMOKE_REQUIRED="generated value $ literal λ",
                               RELEASE_SMOKE_EMPTY="", RELEASE_SMOKE_UNSELECTED="excluded")
            override = root / "images.json"
            override.write_text(json.dumps({"services": {
                "claude-cli": {"image": image},
                "claude-desktop": {"image": project_name + "-desktop:test"}}}))
            commands = []

            class ObservedRunner(CommandRunner):
                def run(self, argv, **kwargs):
                    commands.append(list(argv))
                    return super().run(argv, **kwargs)

            class TestProject(ComposeProject):
                """Only resource names change; config and lifecycle remain real."""
                def __init__(self, *args, **kwargs):
                    kwargs["program_dir"] = ROOT
                    super().__init__(*args, **kwargs)
                    self.project_name = project_name
                    self.files.insert(-1, override)

                def _model(self, mode):
                    return TestProject(replace(self.config, docker_mode=mode), self.paths,
                                       self.mounts, self.runner)

            previous = Path.cwd()
            with (patch.dict(os.environ, environment, clear=True),
                  patch.object(cli, "ComposeProject", TestProject),
                  patch.object(cli, "CommandRunner", ObservedRunner)):
                try:
                    os.chdir(workspace)
                    self.assertEqual(cli.main(["cli", "--version"]), 0)
                    container = self.command("docker", "ps", "-q", "--filter",
                                             f"label=com.docker.compose.project={project_name}")
                    self.assertTrue(container)
                    details = json.loads(self.command("docker", "inspect", container))[0]
                    self.assertEqual(details["Config"]["Image"], image)
                    self.assertEqual(details["Config"]["User"], account.pw_name)
                    self.assertFalse(details["HostConfig"]["Privileged"])
                    self.assertFalse(details["HostConfig"].get("CapAdd"))
                    self.assertFalse(any("unconfined" in option for option in
                                         details["HostConfig"].get("SecurityOpt") or []))
                    self.assertNotEqual(details["HostConfig"].get("Runtime"), "sysbox-runc")
                    self.assertTrue(any(mount["Source"] == str(workspace) and
                                        mount["Destination"] == str(workspace)
                                        for mount in details["Mounts"]))
                    self.assertFalse(any(mount["Destination"] == "/var/run/docker.sock"
                                         for mount in details["Mounts"]))
                    version = self.command("docker", "exec", container, "claude", "--version")
                    self.assertRegex(version, r"\b\d+\.\d+\.\d+.*Claude Code")
                    observed = self.command(
                        "docker", "exec", "--workdir", str(workspace),
                        "--env", "RELEASE_SMOKE_REQUIRED", "--env", "RELEASE_SMOKE_EMPTY",
                        container, "sh", "-ec",
                        'id -u; id -g; printf "%s\\n" "$HOME" "$PWD"; cat marker; '
                        "test \"$RELEASE_SMOKE_REQUIRED\" = 'generated value $ literal λ'; "
                        'test "${RELEASE_SMOKE_EMPTY+x}" = x; test -z "$RELEASE_SMOKE_EMPTY"; '
                        'test "${RELEASE_SMOKE_ABSENT+x}" != x; '
                        'test "${RELEASE_SMOKE_UNSELECTED+x}" != x; '
                        'test "${DOCKER_HOST+x}" != x; test ! -S /var/run/docker.sock')
                    self.assertEqual(observed.splitlines(), [str(os.getuid()), str(os.getgid()),
                                     account.pw_dir, str(workspace), "isolated workspace"])
                    execs = [argv for argv in commands if argv[:2] == ["docker", "exec"]]
                    self.assertEqual(len(execs), 1)
                    self.assertEqual(execs[0][execs[0].index("--workdir") + 1], str(workspace))
                    for name in ("RELEASE_SMOKE_REQUIRED", "RELEASE_SMOKE_EMPTY"):
                        self.assertIn(name, execs[0])
                        self.assertEqual(execs[0][execs[0].index(name) - 1], "--env")
                    self.assertNotIn("RELEASE_SMOKE_ABSENT", execs[0])
                    self.assertNotIn("RELEASE_SMOKE_UNSELECTED", execs[0])
                    self.assertEqual(execs[0][-2:], ["claude", "--version"])
                    self.assertNotIn(environment["RELEASE_SMOKE_REQUIRED"], execs[0])
                    services = self.command("docker", "ps", "-a", "--format", "{{.Label \"com.docker.compose.service\"}}",
                                            "--filter", f"label=com.docker.compose.project={project_name}")
                    self.assertEqual(services, "claude-cli")
                    print(f"CLI launcher: {account.pw_name} {os.getuid()}:{os.getgid()} "
                          f"HOME={account.pw_dir}, workdir={workspace}, {version}; "
                          f"environment and none mode verified; project={project_name}", flush=True)
                finally:
                    try:
                        stop_result = cli.main(["stop"])
                        self.assertEqual(stop_result, 0, "Launcher stop failed")
                        # Qualify production teardown before fallback can affect it.
                        for query in (["docker", "ps", "-aq"], ["docker", "network", "ls", "-q"]):
                            self.assertEqual(self.command(*query, "--filter",
                                             f"label=com.docker.compose.project={project_name}",
                                             timeout=20), "", "Launcher stop left project resources")
                        self.assertFalse((root / "runtime/claude-docker/compose.none.json").exists())
                    finally:
                        try:
                            self.remove_cli_project(project_name)
                        finally:
                            os.chdir(previous)
                    for query in (["docker", "ps", "-aq"], ["docker", "network", "ls", "-q"]):
                        self.assertEqual(self.command(*query, "--filter",
                                         f"label=com.docker.compose.project={project_name}",
                                         timeout=20), "")
        self.assertFalse(root.exists())

    def test_inner_docker_readiness_starts_or_reuses_only_the_dependency(self):
        image = "docker:29-dind"
        if not self.command("docker", "image", "ls", "-q", "--filter", f"reference={image}"):
            self.skipTest(f"local {image} image is required for the disposable sleep services")

        with tempfile.TemporaryDirectory(prefix="claude-inner-readiness-") as temporary:
            root = Path(temporary)
            paths = HostPaths(root / "home", root / "config", root / "data", root / "state",
                              root / "cache", root / "runtime")
            config = load_config(env={}, paths=paths,
                                 account=AccountInfo("tester", 10000, 10000, paths.home))
            runner = CommandRunner()
            disabled = ComposeProject(config, paths, (), runner, program_dir=root)
            with patch.object(runner, "run", side_effect=AssertionError("none mode invoked Docker")):
                disabled.ensure_inner_docker()

            project = ComposeProject(replace(config, docker_mode="sysbox"), paths, (), runner,
                                     program_dir=root)
            project.project_name = "claude-inner-readiness-" + self.token
            keeper = {"image": image, "runtime": "runc", "network_mode": "none",
                      "entrypoint": ["sleep", "infinity"], "tmpfs": ["/var/lib/docker"]}
            (root / "compose.yaml").write_text(json.dumps({"services": {
                "claude-cli": keeper,
                "claude-desktop": {**keeper, "profiles": ["desktop"]},
            }}))

            def write_overlay(revision):
                dependency = {"depends_on": {"inner-docker": {"condition": "service_healthy"}}}
                (root / "compose.sysbox.yaml").write_text(json.dumps({"services": {
                    "claude-cli": dependency,
                    "claude-desktop": dependency,
                    "inner-docker": {**keeper, "environment": {"TEST_REVISION": revision},
                                     "healthcheck": {"test": ["CMD", "true"],
                                                     "interval": "1s", "timeout": "1s", "retries": 3}},
                }}))

            def compose(*args):
                return self.command(*project.command(*args, runtime_mounts=False),
                                    env=project.environment)

            def inspect(service):
                identifier = compose("ps", "--all", "-q", service)
                return json.loads(self.command("docker", "inspect", identifier,
                                               env=project.environment))[0]

            def lifetime(details):
                return details["Id"], details["State"]["StartedAt"], details["RestartCount"]

            write_overlay("initial")
            try:
                compose("up", "-d", "--no-build", "--pull", "never", "--no-deps",
                        "claude-cli", "claude-desktop")
                keepers = ("claude-cli", "claude-desktop")
                original_keepers = [lifetime(inspect(service)) for service in keepers]
                self.assertEqual(compose("ps", "--all", "-q", "inner-docker"), "")

                project.ensure_inner_docker()
                initial = inspect("inner-docker")
                self.assertEqual(initial["State"]["Health"]["Status"], "healthy")
                self.assertEqual([lifetime(inspect(service)) for service in keepers], original_keepers)

                compose("stop", "--timeout", "2", "inner-docker")
                self.assertFalse(inspect("inner-docker")["State"]["Running"])
                # A changed configuration would otherwise cause Compose to replace it.
                write_overlay("changed")
                project.ensure_inner_docker()
                restarted = inspect("inner-docker")
                self.assertEqual(restarted["Id"], initial["Id"])
                self.assertNotEqual(restarted["State"]["StartedAt"], initial["State"]["StartedAt"])
                self.assertEqual(restarted["State"]["Health"]["Status"], "healthy")
                self.assertEqual([lifetime(inspect(service)) for service in keepers], original_keepers)

                project.ensure_inner_docker()
                self.assertEqual(lifetime(inspect("inner-docker")), lifetime(restarted))
                self.assertEqual([lifetime(inspect(service)) for service in keepers], original_keepers)
                self.assertFalse(paths.runtime_dir.exists())
                self.assertFalse(paths.state_dir.exists())
            finally:
                compose("down", "--volumes", "--timeout", "2")
            self.assertEqual(self.command("docker", "ps", "-aq", "--filter",
                                          f"label=com.docker.compose.project={project.project_name}",
                                          env=project.environment), "")

    def test_sysbox_inner_docker(self):
        # Catch a daemon health/TLS/client mount regression with a real nested run.
        # Do not opt in on an experimental host without a separate explicit request.
        require_integration(os.environ, sysbox=True)
        if "sysbox-runc" not in self.docker_info.get("Runtimes", {}):
            self.skipTest("Docker host has no sysbox-runc runtime configured")
        image = self.build_image("cli")
        project = f"claude-integration-{os.getuid()}-{os.getpid()}"
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("COMPOSE_") and not key.startswith("CLAUDE_")
        }
        environment.update(IDENTITY, DOCKER_DEFAULT_PLATFORM=self.build_platform)
        with tempfile.TemporaryDirectory(prefix="claude-sysbox-test-") as directory:
            # Keep smoke builds isolated from the launcher's stable image tag.
            override = Path(directory) / "image.json"
            override.write_text(json.dumps({"services": {"claude-cli": {"image": image}}}))
            compose = ["docker", "compose", "--project-directory", str(ROOT),
                       "--env-file", os.devnull, "--project-name", project,
                       "-f", "compose.yaml", "-f", "compose.sysbox.yaml", "-f", str(override)]
            model = json.loads(self.command(*compose, "config", "--format", "json", env=environment))
            self.assertEqual(set(model["services"]), {"claude-cli", "inner-docker"})
            self.assertEqual(model["services"]["claude-cli"]["image"], image)
            data_volume = model["volumes"]["inner-docker-data"]["name"]
            try:
                self.command(*compose, "up", "-d", "--wait", "--wait-timeout", "120",
                             "inner-docker", timeout=180, env=environment)
                container = self.command(*compose, "ps", "-q", "inner-docker", env=environment)
                self.assertTrue(container)
                health = self.command("docker", "inspect", "--format", "{{.State.Health.Status}}", container)
                self.assertEqual(health, "healthy")
                output = self.command(*compose, "run", "--rm", "--no-deps", "--pull", "never",
                                      "-T", "claude-cli", "docker", "run", "--rm", "hello-world",
                                      timeout=180, env=environment)
                self.assertIn("Hello from Docker!", output)
            finally:
                # Deliberately omit --volumes: daemon state must survive teardown.
                self.command(*compose, "down", "--remove-orphans", "--timeout", "20",
                             timeout=120, env=environment)
            self.assertEqual(self.command("docker", "ps", "-aq", "--filter",
                                          f"label=com.docker.compose.project={project}"), "")
            self.assertEqual(self.command("docker", "network", "ls", "-q", "--filter",
                                          f"label=com.docker.compose.project={project}"), "")
            self.command("docker", "volume", "inspect", data_volume)


if __name__ == "__main__":
    unittest.main()
