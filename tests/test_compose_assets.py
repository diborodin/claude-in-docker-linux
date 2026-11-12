"""Render the real Compose model without creating containers or resources."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.test_compose import ComposeFixture
from claude_docker.model import DesktopRuntime, ProcessIdentity
from claude_docker.seccomp import POLICY_RELATIVE_PATH, chromium_policy_path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ARGS = {
    "CLAUDE_USER": "tester",
    "CLAUDE_UID": "10000",
    "CLAUDE_GID": "10001",
    "CLAUDE_HOME": "/srv/tester",
    "CLAUDE_HOST_UID": "12345",
}
OWNER_LABELS = {
    "io.github.diborodin.claude-in-docker-linux.managed": "true",
    "io.github.diborodin.claude-in-docker-linux.owner-uid": "12345",
}


class ComposeAssetTests(unittest.TestCase):
    def test_sysbox_smoke_requires_both_explicit_opt_ins(self):
        # Either missing authorization must stop the smoke before Docker starts.
        from tests.test_container_integration import require_integration

        for environment in (
            {},
            {"CLAUDE_DOCKER_INTEGRATION": "1"},
            {"CLAUDE_DOCKER_SYSBOX_TEST": "1"},
            {"CLAUDE_DOCKER_INTEGRATION": "1", "CLAUDE_DOCKER_SYSBOX_TEST": "true"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(unittest.SkipTest, "CLAUDE_DOCKER_.*=1"):
                    require_integration(environment, sysbox=True)
        require_integration({
            "CLAUDE_DOCKER_INTEGRATION": "1", "CLAUDE_DOCKER_SYSBOX_TEST": "1",
        }, sysbox=True)

    def compose(self, *arguments, profiles=(), files=("compose.yaml",)):
        # Do not inherit COMPOSE_PROFILES, interpolation values, or a local .env.
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": "/tmp/claude-compose-test-home",
            **BUILD_ARGS,
        }
        command = [
            "docker", "compose", "--project-directory", ".",
            "--env-file", os.devnull,
        ]
        for filename in files:
            command.extend(["-f", filename])
        for profile in profiles:
            command.extend(["--profile", profile])
        result = subprocess.run(
            [*command, *arguments], cwd=ROOT, env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def model(self, *, profiles=(), files=("compose.yaml",)):
        return json.loads(self.compose(
            "config", "--format", "json", profiles=profiles, files=files,
        ))

    def sysbox_model(self, *, profiles=()):
        self.assertTrue((ROOT / "compose.sysbox.yaml").is_file(), "Sysbox overlay is missing")
        return self.model(profiles=profiles, files=("compose.yaml", "compose.sysbox.yaml"))

    def test_base_model_is_portable(self):
        # Catch a combined service, wrong product/identity, or a runtime grant
        # accidentally making ordinary mode depend on host-specific resources.
        model = self.model(profiles=("desktop",))
        self.assertEqual(set(model["services"]), {"claude-cli", "claude-desktop"})
        images = set()
        for name, target in (("claude-cli", "cli"), ("claude-desktop", "desktop")):
            with self.subTest(service=name):
                service = model["services"][name]
                self.assertEqual(service["build"]["context"], str(ROOT))
                self.assertEqual(service["build"]["target"], target)
                self.assertEqual(service["build"]["args"], BUILD_ARGS)
                self.assertEqual(service["command"], ["sleep", "infinity"])
                self.assertIs(service["init"], True)
                self.assertEqual(service["labels"], OWNER_LABELS)
                self.assertIn("12345", service["image"])
                images.add(service["image"])
                for forbidden in (
                    "volumes", "volumes_from", "environment", "env_file",
                    "runtime", "depends_on", "devices", "device_cgroup_rules",
                    "cap_add", "cap_drop", "privileged", "security_opt",
                    "user", "userns_mode", "group_add", "pid", "ipc", "uts",
                    "cgroup", "cgroup_parent", "sysctls", "network_mode", "ports",
                    "configs", "secrets",
                ):
                    self.assertNotIn(forbidden, service)
        self.assertEqual(len(images), 2, "products must not overwrite the same image tag")
        for forbidden in ("volumes", "configs", "secrets"):
            self.assertNotIn(forbidden, model)

    def test_desktop_requires_profile(self):
        # Catch accidental Desktop inclusion in ordinary up/build selection.
        default = self.model()
        self.assertEqual(set(default["services"]), {"claude-cli"})
        self.assertNotIn("profiles", default["services"]["claude-cli"])
        enabled = self.model(profiles=("desktop",))
        self.assertEqual(set(enabled["services"]), {"claude-cli", "claude-desktop"})
        self.assertEqual(enabled["services"]["claude-desktop"]["profiles"], ["desktop"])
        self.assertEqual(self.compose("config", "--profiles").splitlines(), ["desktop"])

    def test_default_network_has_owner_labels(self):
        # Catch an unlabeled network that resource cleanup cannot identify.
        model = self.model(profiles=("desktop",))
        self.assertEqual(set(model["networks"]), {"default"})
        self.assertEqual(model["networks"]["default"].get("labels"), OWNER_LABELS)
        for service in model["services"].values():
            self.assertEqual(set(service["networks"]), {"default"})

    def test_sysbox_overlay_uses_tls_and_health_gate(self):
        # Catch plaintext clients, unusable TLS hostnames, missing persistence,
        # or clients racing a daemon that has not become ready.
        for profiles in ((), ("desktop",)):
            with self.subTest(profiles=profiles):
                model = self.sysbox_model(profiles=profiles)
                clients = {"claude-cli"}
                if profiles:
                    clients.add("claude-desktop")
                self.assertEqual(set(model["services"]), clients | {"inner-docker"})
                daemon = model["services"]["inner-docker"]
                self.assertEqual(daemon["image"], "docker:29-dind")
                self.assertEqual(daemon["runtime"], "sysbox-runc")
                self.assertEqual(daemon["environment"]["DOCKER_TLS_CERTDIR"], "/certs")
                self.assertEqual(daemon["environment"]["DOCKER_TLS_SAN"], "DNS:inner-docker")
                # Preserve the image entrypoint that generates certificates and
                # enables the TLS listener when DOCKER_TLS_CERTDIR is set.
                self.assertIsNone(daemon.get("entrypoint"))
                self.assertIsNone(daemon.get("command"))
                self.assertEqual(daemon["healthcheck"]["test"], [
                    "CMD", "docker", "--host=tcp://localhost:2376", "--tlsverify",
                    "--tlscacert=/certs/client/ca.pem",
                    "--tlscert=/certs/client/cert.pem",
                    "--tlskey=/certs/client/key.pem", "info",
                ])
                self.assertFalse(daemon["healthcheck"].get("disable", False))
                mounts = {mount["target"]: mount for mount in daemon["volumes"]}
                self.assertEqual(set(mounts), {"/var/lib/docker", "/certs/ca", "/certs/client"})
                for target, source in (
                    ("/var/lib/docker", "inner-docker-data"),
                    ("/certs/ca", "inner-docker-ca"),
                    ("/certs/client", "inner-docker-client"),
                ):
                    self.assertEqual(mounts[target]["type"], "volume")
                    self.assertEqual(mounts[target]["source"], source)
                    self.assertFalse(mounts[target].get("read_only", False))
                for name in clients:
                    client = model["services"][name]
                    self.assertEqual(client["environment"], {
                        "DOCKER_HOST": "tcp://inner-docker:2376",
                        "DOCKER_TLS_VERIFY": "1",
                        "DOCKER_CERT_PATH": "/certs/client",
                    })
                    self.assertEqual(set(client["depends_on"]), {"inner-docker"})
                    self.assertEqual(client["depends_on"]["inner-docker"]["condition"],
                                     "service_healthy")
                    self.assertIs(client["depends_on"]["inner-docker"]["required"], True)
                    self.assertEqual(len(client["volumes"]), 1)
                    mount = client["volumes"][0]
                    self.assertEqual(mount["type"], "volume")
                    self.assertEqual(mount["source"], "inner-docker-client")
                    self.assertEqual(mount["target"], "/certs/client")
                    self.assertIs(mount["read_only"], True)
                if profiles:
                    self.assertEqual(model["services"]["claude-desktop"]["profiles"], ["desktop"])

    def test_sysbox_overlay_never_weakens_outer_security(self):
        # Catch outer privilege grants, host sockets/API publication, and any
        # fallback to the unauthenticated Docker port in either selection.
        for profiles in ((), ("desktop",)):
            with self.subTest(profiles=profiles):
                model = self.sysbox_model(profiles=profiles)
                self.assertNotIn("2375", json.dumps(model))
                self.assertNotIn("unconfined", json.dumps(model))
                for name, service in model["services"].items():
                    with self.subTest(service=name):
                        for forbidden in ("privileged", "cap_add", "security_opt", "ports",
                                          "devices", "device_cgroup_rules", "volumes_from",
                                          "network_mode", "pid", "ipc"):
                            self.assertNotIn(forbidden, service)
                        for mount in service["volumes"]:
                            self.assertEqual(mount["type"], "volume")
                            self.assertNotIn("docker.sock", json.dumps(mount))
                        if name != "inner-docker":
                            self.assertNotIn("runtime", service)

    def test_sysbox_volumes_have_owner_labels(self):
        # Catch persistent daemon data or private certificates escaping exact
        # managed/owner selection during a later purge.
        for profiles in ((), ("desktop",)):
            with self.subTest(profiles=profiles):
                model = self.sysbox_model(profiles=profiles)
                self.assertEqual(set(model["volumes"]), {
                    "inner-docker-data", "inner-docker-ca", "inner-docker-client",
                })
                for volume in model["volumes"].values():
                    self.assertEqual(volume["labels"], OWNER_LABELS)
                    self.assertFalse(volume.get("external", False))
                self.assertEqual(model["networks"]["default"]["labels"], OWNER_LABELS)
                for service in model["services"].values():
                    self.assertEqual(service["labels"], OWNER_LABELS)


class RuntimeGrantAssetTests(ComposeFixture, unittest.TestCase):
    def test_rendered_opt_in_preserves_literal_policy_path_and_desktop_only_grant(self):
        # Exercise our escaping at Compose's rendered boundary, without a daemon.
        program = self.root / 'program space $HOME,comma'
        policy = program / POLICY_RELATIVE_PATH
        policy.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / POLICY_RELATIVE_PATH, policy)
        validated = chromium_policy_path(program)
        runtime = DesktopRuntime(25, self.root / 'x11', self.root / 'Xauthority',
                                 ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        config = replace(self.config, container=replace(self.config.container, uid=os.getuid()))
        project = self.project(config=config, desktop_runtime=runtime, desktop_seccomp=validated)
        for mode, files in (('none', ('compose.yaml',)),
                            ('sysbox', ('compose.yaml', 'compose.sysbox.yaml'))):
            with self.subTest(mode=mode):
                override = project._model(mode).write_override()
                model = ComposeAssetTests().model(profiles=('desktop',), files=(*files, str(override)))
                for name, service in model['services'].items():
                    if name == 'claude-desktop':
                        # `compose config` re-escapes literal dollars on output.
                        # Decode that transport escape once to inspect semantic grants.
                        self.assertEqual([value.replace('$$', '$') for value in service['security_opt']],
                                         ['seccomp=' + str(policy)])
                    else:
                        self.assertNotIn('security_opt', service)
                    for forbidden in ('privileged', 'cap_add', 'devices', 'ports', 'network_mode', 'pid', 'ipc'):
                        self.assertNotIn(forbidden, service)
                for forbidden in ('docker.sock', '2375', 'unconfined'):
                    self.assertNotIn(forbidden, json.dumps(model))

    def test_generated_models_grant_only_configured_binds_and_private_desktop_x11(self):
        # Unlike a textual ban on /tmp/.X11-unix, inspect host sources and allow
        # the nested container target while refusing the entire host X directory.
        private = self.root / 'private-x11'
        private.mkdir(mode=0o700)
        authority = self.root / 'Xauthority'
        authority.touch(mode=0o600)
        runtime = DesktopRuntime(25, private, authority,
                                 ProcessIdentity(12345, 100, '/usr/bin/Xephyr'))
        allowed = {str(self.workspace).replace('$', '$$'), str(self.config.state_code.source),
                   str(self.config.state_code_json.source), str(self.config.state_desktop.source)}
        for mode in ('none', 'sysbox'):
            for desktop in (None, runtime):
                with self.subTest(mode=mode, desktop=desktop is not None):
                    config = replace(self.config, docker_mode=mode,
                                     container=replace(self.config.container, uid=os.getuid()))
                    project = self.project(config=config, desktop_runtime=desktop)
                    model = json.loads(project.write_override().read_text())
                    self.assertEqual(set(model['services']), {'claude-cli', 'claude-desktop'})
                    for name, service in model['services'].items():
                        for forbidden in ('privileged', 'security_opt', 'cap_add', 'devices',
                                          'ports', 'network_mode', 'pid', 'ipc', 'env_file'):
                            self.assertNotIn(forbidden, service)
                        sources = {mount['source'] for mount in service['volumes']}
                        extra = {str(private), str(authority)} if desktop and name == 'claude-desktop' else set()
                        expected = allowed if name == 'claude-desktop' else allowed - {
                            str(self.config.state_desktop.source)}
                        self.assertEqual(sources, expected | extra)
                        self.assertNotIn('/tmp/.X11-unix', sources)
                        for mount in service['volumes']:
                            self.assertEqual(mount['type'], 'bind')
                            self.assertIs(mount['bind']['create_host_path'], False)
                        if extra:
                            grants = {mount['target']: mount for mount in service['volumes']
                                      if mount['source'] in extra}
                            self.assertEqual(set(grants), {'/tmp/.X11-unix', '/run/claude-docker/Xauthority'})
                            self.assertEqual({mount['source'] for mount in grants.values()}, extra)
                            self.assertTrue(all(mount['read_only'] for mount in grants.values()))
                        environment = {'HOME': '/home/tester'}
                        if extra:
                            environment.update(DISPLAY=':25', XAUTHORITY='/run/claude-docker/Xauthority')
                        self.assertEqual(service['environment'], environment)
                    text = json.dumps(model)
                    for forbidden in ('docker.sock', '2375', 'unconfined'):
                        self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
