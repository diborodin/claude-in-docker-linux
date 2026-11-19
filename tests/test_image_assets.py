"""Build-interface checks; runtime behavior is verified with real image builds."""

import fnmatch
import os
from pathlib import Path
import re
import subprocess
import tempfile
import shlex
import tomllib
import unittest
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


def dockerfile_instructions():
    """Read logical instructions without depending on indentation or wrapping."""
    source = (ROOT / "Dockerfile").read_text()
    source = re.sub(r"\\\s*\n", " ", source)
    return [
        (parts[0].upper(), parts[1].strip())
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for parts in [line.strip().split(None, 1)]
    ]


class ImageAssetTests(unittest.TestCase):
    def test_container_integration_requires_explicit_opt_in(self):
        # Catch an absent or truthy-but-not-1 flag accidentally enabling builds.
        from tests.test_container_integration import require_integration

        for value in (None, "", "0", "true"):
            environment = {} if value is None else {"CLAUDE_DOCKER_INTEGRATION": value}
            with self.subTest(value=value):
                with self.assertRaisesRegex(unittest.SkipTest, "CLAUDE_DOCKER_INTEGRATION=1"):
                    require_integration(environment)
        require_integration({"CLAUDE_DOCKER_INTEGRATION": "1"})

    def test_common_and_cli_stage_contract(self):
        # Catch a missing product target, lost identity argument, root final
        # user, or labels that would make resource ownership incorrect.
        instructions = dockerfile_instructions()
        stages = {}
        current = None
        for operation, value in instructions:
            if operation == "FROM":
                image, keyword, name = shlex.split(value)
                self.assertEqual(keyword.upper(), "AS")
                current = stages[name.lower()] = {"image": image, "instructions": []}
            elif current is not None:
                current["instructions"].append((operation, value))

        self.assertIn("mise", stages)
        self.assertEqual(stages["mise"]["image"], "jdxcode/mise:2026.11.5")
        self.assertIn("common", stages)
        self.assertEqual(stages["common"]["image"], "ubuntu:26.04")
        common_runs = " ".join(value for op, value in stages["common"]["instructions"] if op == "RUN")
        self.assertIn("https://download.docker.com/linux/ubuntu resolute stable", common_runs)
        mise_copies = [
            fields[1:]
            for op, value in stages["common"]["instructions"] if op == "COPY"
            for fields in [shlex.split(value)] if fields[0] == "--from=mise"
        ]
        self.assertIn(["/usr/local/bin/mise", "/usr/local/bin/mise"], mise_copies)
        self.assertIn("cli", stages)
        self.assertEqual(stages["cli"]["image"], "common")

        inherited = stages["common"]["instructions"] + stages["cli"]["instructions"]
        arguments = {value.split("=", 1)[0] for op, value in inherited if op == "ARG"}
        self.assertTrue(
            {"CLAUDE_USER", "CLAUDE_UID", "CLAUDE_GID", "CLAUDE_HOME", "CLAUDE_HOST_UID"}
            <= arguments
        )
        users = [value for op, value in inherited if op == "USER"]
        self.assertIn(users[-1], ("${CLAUDE_USER}", "$CLAUDE_USER"))
        labels = dict(
            field.split("=", 1)
            for op, value in stages["cli"]["instructions"] if op == "LABEL"
            for field in shlex.split(value)
        )
        self.assertEqual(labels["io.github.diborodin.claude-in-docker-linux.managed"], "true")
        self.assertIn(
            labels["io.github.diborodin.claude-in-docker-linux.owner-uid"],
            ("${CLAUDE_HOST_UID}", "$CLAUDE_HOST_UID"),
        )
        self.assertNotIn("debian:stable-slim", [stage["image"] for stage in stages.values()])

    def stages(self):
        stages = {}
        current = None
        for operation, value in dockerfile_instructions():
            if operation == "FROM":
                image, _, name = shlex.split(value)
                current = stages[name] = {"image": image, "instructions": []}
            elif current is not None:
                current["instructions"].append((operation, value))
        return stages

    def test_tool_assets_are_locked_for_both_platforms(self):
        # Missing platform/checksum data must fail before a build downloads tools.
        config_path = ROOT / "image/mise.toml"
        lock_path = ROOT / "image/mise.lock"
        self.assertTrue(config_path.is_file(), "declarative tool config is missing")
        self.assertTrue(lock_path.is_file(), "generated tool lock is missing")
        config = tomllib.loads(config_path.read_text())
        self.assertEqual(config["bootstrap"]["packages"]["apt:jq"], "latest")
        self.assertEqual(config["bootstrap"]["packages"]["apt:less"], "latest")
        self.assertIn('apt:bsdextrautils', config['bootstrap']['packages'])
        self.assertNotIn('kubectl', config.get('tools', {}))
        lock = tomllib.loads(lock_path.read_text())
        self.assertEqual(set(config['tools']), {
            'fd', 'bat', 'ast-grep', 'yq', 'gh', 'uv', 'difftastic', 'aqua:boyter/scc', 'duckdb'})
        self.assertEqual(set(config['bootstrap']['packages']), {
            'apt:' + name for name in ('ripgrep', 'jq', 'shellcheck', 'shfmt', 'sd',
                                      'miller', 'hyperfine', 'bats', 'less', 'bsdextrautils')})
        self.assertEqual(set(lock.get('tools', {})), set(config['tools']))

    def test_every_declared_tool_has_matching_verified_platform_downloads(self):
        # A new declared tool or architecture URL must not silently escape locking.
        config = tomllib.loads((ROOT / 'image/mise.toml').read_text())
        lock = tomllib.loads((ROOT / 'image/mise.lock').read_text())
        self.assertTrue(config['settings']['lockfile'])
        self.assertTrue(config['settings']['locked_require_checksum'])
        self.assertEqual(set(lock.get('tools', {})), set(config['tools']))
        architectures = {'linux-x64': 'amd64', 'linux-arm64': 'arm64'}
        for tool, entries in lock.get('tools', {}).items():
            self.assertTrue(entries, tool)
            for entry in entries:
                with self.subTest(tool=tool, version=entry['version']):
                    self.assertIn(config['tools'][tool], entry['specifiers'])
                    platforms = {key.removeprefix('platforms.'): value for key, value in entry.items()
                                 if key.startswith('platforms.')}
                    self.assertEqual(set(platforms), set(architectures))
                    for platform, architecture in architectures.items():
                        download = platforms[platform]
                        url = urlparse(download['url'])
                        self.assertEqual(url.scheme, 'https')
                        self.assertTrue(url.netloc)
                        self.assertRegex(download['checksum'], r'^sha256:[a-f0-9]{64}$')
                        if tool == 'kubectl':
                            self.assertEqual(url.netloc, 'dl.k8s.io')
                            self.assertEqual(url.path, f"/v{entry['version']}/bin/linux/{architecture}/kubectl")

    def test_common_docker_client_includes_buildx(self):
        # Removing Buildx breaks BuildKit builds even with a usable Docker client.
        runs = " ".join(value for op, value in self.stages()["common"]["instructions"]
                        if op == "RUN")
        self.assertRegex(runs, r"apt-get install [^&;]*\bdocker-buildx-plugin\b")
        self.assertIn("https://download.docker.com/linux/ubuntu", runs)

    def test_claude_system_binary_survives_home_mount(self):
        # A home-local symlink or earlier home PATH entry hides/replaces image Code.
        stages = self.stages()
        common = stages["common"]["instructions"]
        stage = stages.get("claude-code")
        self.assertIsNotNone(stage, "independent Code stage is missing")
        self.assertEqual(stage["image"], "ubuntu:26.04")
        instructions = stage["instructions"]
        pin = next(index for index, (op, value) in enumerate(instructions)
                   if op == "COPY" and value.startswith("image/claude-code.version "))
        prerequisites = [(index, value) for index, (op, value) in enumerate(instructions)
                         if op == "RUN" and "apt-get install" in value]
        self.assertEqual(len(prerequisites), 1)
        self.assertLess(prerequisites[0][0], pin)
        fetch = " ".join(value for op, value in instructions[pin + 1:] if op == "RUN")
        for token in ("downloads.claude.ai/claude-code-releases", "/manifest.json",
                      "$platform/claude", "linux-x64", "linux-arm64", "sha256sum", "0555",
                      ".platforms[$platform].checksum"):
            with self.subTest(token=token):
                self.assertIn(token, fetch)
        self.assertEqual((ROOT / 'image/claude-code.version').read_bytes(), b'latest\n')
        self.assertIn('if [ "$version" = latest ]', fetch)
        self.assertIn('"$releases/latest"', fetch)
        self.assertIn('version="$(read_version "$numeric")"', fetch)
        self.assertIn("--proto '=https' --proto-redir '=https'", fetch)
        self.assertNotRegex(fetch, r"install\.sh|npm")
        for name in ("common", "desktop-runtime", "cli", "desktop"):
            instructions = stages[name]["instructions"]
            source = " ".join(value for _, value in instructions)
            self.assertNotRegex(source, r"claude-code\.version|claude\.ai/install\.sh")
            if name not in ("cli", "desktop"):
                self.assertNotIn("DISABLE_UPDATES", source)
                self.assertNotIn("/usr/local/bin/claude ", source)
                continue
            copies = [(i, shlex.split(value)) for i, (op, value) in enumerate(instructions)
                      if op == "COPY" and "/usr/local/bin/claude" in value]
            self.assertEqual(len(copies), 1)
            index, fields = copies[0]
            self.assertEqual(fields, ["--from=claude-code", "--chmod=0555",
                                      "/out/claude", "/usr/local/bin/claude"])
            self.assertLess(max(i for i, (op, _) in enumerate(instructions) if op == "RUN"), index)
            env_index = instructions.index(("ENV", "DISABLE_UPDATES=1"))
            self.assertGreater(env_index, max(i for i, (op, _) in enumerate(instructions) if op == "RUN"))
            self.assertEqual([value for op, value in instructions if op == "USER"][-1], "${CLAUDE_USER}")
        environment = dict(field.split("=", 1) for op, value in common if op == "ENV"
                           for field in shlex.split(value))
        paths = environment["PATH"].split(":")
        self.assertEqual(paths[0], "/usr/local/bin")
        self.assertIn("/usr/local/share/mise/shims", paths)
        self.assertIn("${PATH}", paths)
        self.assertLess(paths.index("/usr/local/bin"), paths.index("${CLAUDE_HOME}/.local/bin"))

    def test_cli_has_no_gui_layer(self):
        # A GUI install in an ancestor would make every CLI pull include it.
        stages = self.stages()
        self.assertEqual(stages["cli"]["image"], "common")
        for name in ("common", "cli"):
            content = " ".join(value for _, value in stages[name]["instructions"])
            self.assertNotRegex(content, r"claude-desktop|openbox|fonts-|libx11|libgtk|libnss3")
        self.assertTrue(any(op == "RUN" and "mise install" in value
                            for op, value in stages["cli"]["instructions"]),
                        "CLI shared tools are missing")

    def test_desktop_is_a_gui_superset(self):
        # Desktop must inherit common, install GUI before tools, and retain ownership.
        stages = self.stages()
        self.assertIn("desktop-runtime", stages)
        self.assertIn("desktop", stages)
        self.assertEqual(stages["desktop-runtime"]["image"], "common")
        self.assertEqual(stages["desktop"]["image"], "desktop-runtime")
        runtime = " ".join(value for _, value in stages["desktop-runtime"]["instructions"])
        for required in ("openbox", "fonts-", "libx11", "libgtk", "xdg-desktop-portal"):
            self.assertIn(required, runtime)
        self.assertIn("<decor>no</decor>", runtime)
        self.assertIn("<maximized>yes</maximized>", runtime)
        for name in ("cli", "desktop"):
            labels = dict(field.split("=", 1)
                          for op, value in stages[name]["instructions"] if op == "LABEL"
                          for field in shlex.split(value))
            self.assertEqual(labels["io.github.diborodin.claude-in-docker-linux.managed"], "true")
            self.assertEqual(labels["io.github.diborodin.claude-in-docker-linux.owner-uid"],
                             "${CLAUDE_HOST_UID}")
            users = [value for op, value in stages[name]["instructions"] if op == "USER"]
            self.assertEqual(users[0], "root")
            self.assertEqual(users[-1], "${CLAUDE_USER}")
        # Symmetric tool instructions must not drift between products.
        tool_ops = lambda name: [(op, value) for op, value in stages[name]["instructions"]
                                if op in ("COPY", "RUN") and 'dpkg -i' not in value]
        self.assertEqual(tool_ops("cli"), tool_ops("desktop"))

    def test_desktop_package_installation_is_late_and_has_no_network(self):
        stages = self.stages()
        desktop = stages['desktop']['instructions']
        runtime = stages['desktop-runtime']['instructions']
        package = stages.get('claude-desktop-package')
        self.assertIsNotNone(package, 'Desktop package stage is missing')
        self.assertIn(('COPY', 'image/claude-desktop.version /tmp/claude-desktop.version'),
                      package['instructions'])
        self.assertIn(('COPY', '--from=mise /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt'),
                      package['instructions'])
        self.assertFalse(any(op == 'RUN' and ' bootstrap' in value for op, value in package['instructions']))
        self.assertFalse(any(re.search(r'\sclaude-desktop(?:\s|$)|downloads\.claude\.ai|dpkg -i', value)
                             for op, value in runtime if op == 'RUN'))
        customization = next(i for i, (op, value) in enumerate(desktop)
                             if op == 'RUN' and 'mise install' in value)
        install = next(i for i, (op, value) in enumerate(desktop)
                       if op == 'RUN' and 'dpkg -i' in value)
        self.assertLess(customization, install)
        self.assertIn('--network=none', desktop[install][1])
        self.assertIn('from=claude-desktop-package', desktop[install][1])
        self.assertFalse(any('claude-desktop-package' in value for _, value in
                             stages['cli']['instructions']))

    def test_baseline_neutral_user_input_is_valid_toml(self):
        run = next(value for op, value in self.stages()['cli']['instructions']
                   if op == 'RUN' and '/opt/user-tools' in value)
        setup = run.split('if [ -f /opt/user-tools/mise-user.lock ]', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(['sh', '-ec', setup.replace('/opt/user-tools', temporary)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(tomllib.loads((Path(temporary) / 'config.toml').read_text()), {'tools': {}})

    def test_customize_hook_is_safe_default(self):
        # The default root hook must perform no install or filesystem mutation.
        hook = ROOT / "image/customize.sh"
        self.assertTrue(hook.is_file(), "customization hook is missing")
        self.assertTrue(os.access(hook, os.X_OK))
        source = hook.read_text()
        self.assertTrue(source.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", source)
        self.assertIn("root", source)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([str(hook)], cwd=directory, capture_output=True, text=True,
                                    env={"PATH": "/usr/bin:/bin", "HOME": directory}, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(Path(directory).iterdir()), [])
        commands = [line for line in source.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(commands, ["set -euo pipefail"])

    def test_system_mise_config_is_not_hidden_by_home_mount(self):
        # Using global/home config or installs would hide kubectl under a home bind mount.
        for name in ("cli", "desktop"):
            stage = self.stages().get(name)
            self.assertIsNotNone(stage, f"{name} product is missing")
            copies = [shlex.split(value) for op, value in stage["instructions"] if op == "COPY"]
            self.assertIn(["image/mise.toml", "/etc/mise/config.toml"], copies)
            self.assertIn(["image/mise.lock", "/etc/mise/mise.lock"], copies)
            self.assertIn(['image/customize.sh', 'image/customize-user.*', '/usr/local/lib/claude-image/'], copies)
            runs = " ".join(value for op, value in stage["instructions"] if op == "RUN")
            self.assertIn(['image/mise.toml', 'image/mise-user.*', '/opt/user-tools/'], copies)
            self.assertIn('MISE_CONFIG_DIR=/opt/user-tools', (ROOT / 'Dockerfile').read_text())
            self.assertIn('MISE_LOCKED=1', (ROOT / 'Dockerfile').read_text())
            self.assertIn('chmod -R a+rX /opt/user-tools /etc/mise', runs)
            self.assertIn("mise bootstrap packages apply", runs)
            self.assertRegex(runs, r"mise install (?=[^&]*--locked)(?=[^&]*--system)")
            self.assertIn("mise reshim --system", runs)
            self.assertLess(runs.index("mise reshim --system"), runs.index("/usr/local/lib/claude-image/customize.sh"))
            user_hook = 'bash -euo pipefail /usr/local/lib/claude-image/customize-user.sh'
            self.assertLess(runs.index('/usr/local/lib/claude-image/customize.sh'), runs.index(user_hook))
            user = next(value for op, value in stage['instructions'] if op == 'USER')
            self.assertEqual(user, 'root')
            self.assertNotIn("--global", runs)
        env = " ".join(value for op, value in self.stages()["common"]["instructions"] if op == "ENV")
        self.assertIn("/usr/local/share/mise/shims", env)

    def test_dockerignore_is_allowlist(self):
        # A new or credential-bearing path must be excluded unless explicitly admitted.
        path = ROOT / ".dockerignore"
        self.assertTrue(path.is_file(), "build-context allowlist is missing")
        patterns = [line.strip() for line in path.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(patterns[0], "**")
        allowed = {pattern[1:] for pattern in patterns[1:] if pattern.startswith("!")}
        self.assertEqual(allowed, {"Dockerfile", "image/mise.toml", "image/mise.lock", "image/customize.sh",
                                   "image/mise-user.toml", "image/mise-user.lock", "image/customize-user.sh",
                                   "image/claude-code.version", "image/claude-desktop.version",
                                   "image/claude-desktop-apt.sh", "image/mise-user-graphs/**/uv.lock", "image/mise-user-graphs/**/pyproject.toml"})
        self.assertEqual(len(patterns), 13)
        for denied in (".git/config", ".claude/credentials.json", ".aws/credentials", "docs/private.md",
                       "workspace/secret", "image/secret", "README.md", "image/customize.example.sh"):
            self.assertFalse(any(fnmatch.fnmatchcase(denied, pattern.rstrip("/")) for pattern in allowed), denied)
