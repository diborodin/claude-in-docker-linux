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
        self.assertEqual(stages["common"]["image"], "ubuntu:24.04")
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
        self.assertIn("kubectl", config["tools"])
        lock = tomllib.loads(lock_path.read_text())
        self.assertIn("kubectl", lock["tools"])
        self.assertTrue(lock["tools"]["kubectl"], "kubectl has no locked versions")
        for entry in lock["tools"]["kubectl"]:
            self.assertTrue(entry["version"])
            platforms = {key.removeprefix("platforms."): value for key, value in entry.items()
                         if key.startswith("platforms.")}
            self.assertEqual(set(platforms), {"linux-x64", "linux-arm64"})
            for data in platforms.values():
                self.assertTrue(data["url"].startswith("https://"))
                self.assertRegex(data["checksum"], r"^sha256:[a-f0-9]{64}$")

    def test_every_declared_tool_has_matching_verified_platform_downloads(self):
        # A new declared tool or architecture URL must not silently escape locking.
        config = tomllib.loads((ROOT / 'image/mise.toml').read_text())
        lock = tomllib.loads((ROOT / 'image/mise.lock').read_text())
        self.assertTrue(config['settings']['lockfile'])
        self.assertTrue(config['settings']['locked_require_checksum'])
        self.assertEqual(set(lock['tools']), set(config['tools']))
        architectures = {'linux-x64': 'amd64', 'linux-arm64': 'arm64'}
        for tool, entries in lock['tools'].items():
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
        common = self.stages()["common"]["instructions"]
        user = "root"
        installer_index = promotion_index = None
        for index, (op, value) in enumerate(common):
            if op == "USER":
                user = value
            elif op == "RUN" and "bash /tmp/claude-install.sh" in value:
                self.assertEqual(user, "${CLAUDE_USER}")
                self.assertIn("https://claude.ai/install.sh", value)
                installer_index = index
            elif op == "RUN" and "/usr/local/bin/claude" in value:
                self.assertEqual(user, "root")
                self.assertIn('readlink -f "${CLAUDE_HOME}/.local/bin/claude"', value)
                self.assertRegex(value, r"install -m 0?555\b")
                promotion_index = index
        self.assertIsNotNone(installer_index, "official unprivileged installer is missing")
        self.assertIsNotNone(promotion_index, "resolved native Code is not promoted outside home")
        self.assertLess(installer_index, promotion_index)
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
        # Desktop must inherit Code/common, install GUI before tools, and retain ownership.
        stages = self.stages()
        self.assertIn("desktop-runtime", stages)
        self.assertIn("desktop", stages)
        self.assertEqual(stages["desktop-runtime"]["image"], "common")
        self.assertEqual(stages["desktop"]["image"], "desktop-runtime")
        runtime = " ".join(value for _, value in stages["desktop-runtime"]["instructions"])
        for required in ("claude-desktop", "openbox", "fonts-", "libx11", "libgtk"):
            self.assertIn(required, runtime)
        self.assertIn("https://downloads.claude.ai/claude-desktop/key.asc", runtime)
        self.assertIn("https://downloads.claude.ai/claude-desktop/apt/stable", runtime)
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
                                if op in ("COPY", "RUN")]
        self.assertEqual(tool_ops("cli"), tool_ops("desktop"))

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
            self.assertIn(["image/customize.sh", "/usr/local/lib/claude-image/customize.sh"], copies)
            runs = " ".join(value for op, value in stage["instructions"] if op == "RUN")
            self.assertIn("mise bootstrap packages apply", runs)
            self.assertRegex(runs, r"mise install (?=[^&]*--locked)(?=[^&]*--system)")
            self.assertIn("mise reshim --system", runs)
            self.assertLess(runs.index("mise reshim --system"), runs.index("/usr/local/lib/claude-image/customize.sh"))
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
        self.assertEqual(allowed, {"Dockerfile", "image/mise.toml", "image/mise.lock", "image/customize.sh"})
        self.assertEqual(len(patterns), 5)
        for denied in (".git/config", ".claude/credentials.json", ".aws/credentials", "docs/private.md",
                       "workspace/secret", "image/secret", "README.md"):
            self.assertFalse(any(fnmatch.fnmatchcase(denied, pattern.rstrip("/")) for pattern in allowed), denied)
