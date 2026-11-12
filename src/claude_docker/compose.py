"""Structured Compose overrides and mode-aware Docker orchestration."""

from collections.abc import Sequence
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from .commands import CommandRunner
from .model import (AppConfig, BindMount, ContainerConfig, DesktopConfig, DesktopRuntime,
                    EnvironmentConfig, StateMountConfig)
from .paths import HostPaths, _private_runtime, resolve_program_dir
from .process import docker_exec_argv
from .state import RuntimeState


# Keep Docker client configuration usable while excluding arbitrary application
# credentials and ambient Compose settings. Container credentials use docker exec.
_DOCKER_ENVIRONMENT = (
    'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT',
    'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
    'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
)
_SERVICES = ('claude-cli', 'claude-desktop')


def _literal(value: str) -> str:
    """Protect literal dollars from Compose's interpolation pass."""
    return value.replace('$', '$$')


def validate_desktop_uid(config: AppConfig, host_uid: int) -> None:
    if config.container.uid != host_uid:
        raise ValueError(f'Desktop requires container.uid to equal host UID {host_uid} for private '
                         f'X11 grants; set container.uid = {host_uid} or remove its override. '
                         'CLI commands support differing container UIDs.')


class ComposeProject:
    def __init__(self, config: AppConfig, paths: HostPaths,
                 mounts: Sequence[BindMount], runner: CommandRunner, *,
                 program_dir: Path | None = None, desktop_runtime: DesktopRuntime | None = None,
                 desktop_seccomp: Path | None = None):
        self.config = config
        self._teardown_only = False
        self.desktop_runtime = desktop_runtime
        self.desktop_seccomp = desktop_seccomp
        self.paths = paths
        self.mounts = tuple(mounts)
        self.runner = runner
        self.program_dir = (program_dir if program_dir is not None else resolve_program_dir(
            Path(__file__).resolve().parents[2] / 'claude-docker', os.environ))
        self.host_uid = os.getuid()
        self.project_name = f'claude-docker-{self.host_uid}'
        self.override_path = paths.runtime_dir / f'compose.{config.docker_mode}.json'
        self.files = [self.program_dir / 'compose.yaml']
        if config.docker_mode == 'sysbox':
            self.files.append(self.program_dir / 'compose.sysbox.yaml')
        self.files.append(self.override_path)
        self.scalars = {
            'CLAUDE_USER': config.container.user,
            'CLAUDE_UID': str(config.container.uid),
            'CLAUDE_GID': str(config.container.gid),
            'CLAUDE_HOME': str(config.container.home),
            'CLAUDE_HOST_UID': str(self.host_uid),
        }
        self.environment = {key: os.environ[key] for key in _DOCKER_ENVIRONMENT if key in os.environ}
        self.environment.update(self.scalars)
        self.environment['COMPOSE_DISABLE_ENV_FILE'] = '1'
        self.state = RuntimeState(paths.state_dir)

    @classmethod
    def for_teardown(cls, paths: HostPaths, runner: CommandRunner) -> 'ComposeProject':
        """Recover owned resources without reading config or preparing launch state.

        Safe interpolation placeholders need not be a launch-valid host account.
        Only mount-free stop/down are permitted on this recovery model.
        """
        unused_state = StateMountConfig(paths.home, False)
        config = AppConfig('none', ContainerConfig('claude', os.getuid(), os.getgid(),
                           Path('/home/claude'), ()), unused_state, unused_state, unused_state,
                           DesktopConfig(True, 'manual'), EnvironmentConfig((), ()))
        project = cls(config, paths, (), runner, program_dir=Path(__file__).resolve().parents[2])
        project._teardown_only = True
        return project

    def command(self, *args: str, runtime_mounts: bool = True) -> list[str]:
        command = ['docker', 'compose', '--project-name', self.project_name]
        for path in (self.files if runtime_mounts else self.files[:-1]):
            command.extend(['-f', str(path)])
        if 'claude-desktop' in args or 'down' in args or 'rm' in args:
            command.extend(['--profile', 'desktop'])
        return command + list(args)

    def _validate_mounts(self) -> None:
        file_target = self.config.container.home / '.claude.json'
        for mount in self.mounts:
            is_file = mount.target == file_target and mount.source == self.config.state_code_json.source
            if not mount.source.is_absolute() or not mount.target.is_absolute():
                raise ValueError(f'Bind source and target must be absolute: {mount.source}')
            if not (mount.source.is_file() if is_file else mount.source.is_dir()):
                kind = 'file' if is_file else 'directory'
                raise ValueError(f'Bind source must be an existing {kind}: {mount.source}')

    def write_override(self) -> Path:
        if self._teardown_only:
            raise ValueError('Recovery project permits mount-free teardown only')
        if self.desktop_runtime is not None:
            validate_desktop_uid(self.config, self.host_uid)
        self._validate_mounts()
        services = {}
        desktop_target = self.config.container.home / '.config/Claude'
        labels = {'io.github.diborodin.claude-in-docker-linux.managed': 'true',
                  'io.github.diborodin.claude-in-docker-linux.owner-uid': str(self.host_uid)}
        for name in _SERVICES:
            volumes = []
            for mount in self.mounts:
                if (name == 'claude-cli' and mount.target == desktop_target
                        and mount.source == self.config.state_desktop.source):
                    continue
                volumes.append({'type': 'bind', 'source': _literal(str(mount.source)),
                                'target': _literal(str(mount.target)), 'read_only': mount.read_only,
                                'bind': {'create_host_path': False}})
            services[name] = {
                'build': {'args': {key: _literal(value) for key, value in self.scalars.items()}},
                'environment': {'HOME': _literal(str(self.config.container.home))},
                'labels': labels,
                'volumes': volumes,
            }
        if self.desktop_runtime is not None:
            runtime = self.desktop_runtime
            if self.desktop_seccomp is not None:
                services['claude-desktop']['security_opt'] = [
                    'seccomp=' + _literal(str(self.desktop_seccomp))]
            services['claude-desktop']['environment'].update({
                'DISPLAY': f':{runtime.display}', 'XAUTHORITY': '/run/claude-docker/Xauthority'})
            for source, target in ((runtime.socket_dir, '/tmp/.X11-unix'),
                                   (runtime.xauthority, '/run/claude-docker/Xauthority')):
                services['claude-desktop']['volumes'].append({
                    'type': 'bind', 'source': _literal(str(source)), 'target': target,
                    'read_only': True, 'bind': {'create_host_path': False}})
        _private_runtime(self.paths.runtime_dir, self.host_uid)
        descriptor, temporary = tempfile.mkstemp(prefix='.compose-', suffix='.json',
                                                 dir=self.paths.runtime_dir)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'services': services}, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.replace(temporary, self.override_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.override_path

    def _run(self, *args: str, capture_output: bool = False, runtime_mounts: bool = True):
        if self._teardown_only and (runtime_mounts or not args or args[0] not in ('stop', 'down', 'rm')):
            raise ValueError('Recovery project permits mount-free teardown only')
        if runtime_mounts:
            self.write_override()
        return self.runner.run(self.command(*args, runtime_mounts=runtime_mounts), env=self.environment,
                               capture_output=capture_output)

    @staticmethod
    def _service(service: str) -> None:
        if service not in _SERVICES:
            raise ValueError('Select claude-cli or claude-desktop')

    def _model(self, mode: str) -> 'ComposeProject':
        project = ComposeProject(replace(self.config, docker_mode=mode), self.paths,
                                 self.mounts, self.runner, program_dir=self.program_dir,
                                 desktop_runtime=self.desktop_runtime, desktop_seccomp=self.desktop_seccomp)
        project._teardown_only = self._teardown_only
        return project

    def up(self, service: str, *, recreate: bool = False) -> None:
        self._service(service)
        # Validate the desired model before stopping a working old model.
        self.write_override()
        old = self.state.read_mode()
        pending = self.state.read_pending()
        if pending is not None:
            self._model(pending)._run('down', runtime_mounts=False)
            self.state.clear_pending()
        if old is not None and old != self.config.docker_mode and old != pending:
            self._model(old)._run('down', runtime_mounts=False)
        # Compose may create resources before failing on dependency health.
        # Persist recovery information before up, without claiming success.
        self.state.write_pending(self.config.docker_mode)
        options = ['up', '-d']
        if recreate:
            options.append('--force-recreate')
            if old == self.config.docker_mode and pending is None:
                options.append('--no-deps')
        self._run(*options, service)
        self.state.write_mode(self.config.docker_mode)
        self.state.clear_pending()

    def running_container_id(self, service: str, *, runtime_mounts: bool = True) -> str | None:
        self._service(service)
        output = self._run('ps', '-q', service, capture_output=True,
                           runtime_mounts=runtime_mounts).stdout
        identifiers = [line.strip() for line in output.splitlines() if line.strip()]
        if not identifiers:
            return None
        if len(identifiers) != 1:
            raise ValueError(f'Expected exactly one running container for {service}; '
                             'start the service and retry')
        return identifiers[0]

    def container_id(self, service: str) -> str:
        identifier = self.running_container_id(service)
        if identifier is None:
            raise ValueError(f'Expected exactly one running container for {service}; '
                             'start the service and retry')
        return identifier

    def stop_service(self, service: str) -> None:
        self._service(service)
        self._run('stop', service, runtime_mounts=False)

    def remove_containers(self) -> None:
        """Remove owned project containers, retaining networks/images/volumes."""
        self._teardown('rm', '--stop', '--force')

    def down(self) -> None:
        self._teardown('down')

    def _teardown(self, *operation: str) -> None:
        modes = tuple(dict.fromkeys(mode for mode in (
            self.state.read_pending(), self.state.read_mode()) if mode is not None))
        for mode in modes or (self.config.docker_mode,):
            self._model(mode)._run(*operation, runtime_mounts=False)
        # Never create runtime/application directories during teardown, or follow
        # a substituted runtime symlink when deleting generated files.
        try:
            descriptor = os.open(self.paths.runtime_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            try:
                if os.fstat(descriptor).st_uid != self.host_uid:
                    raise ValueError(f'Private runtime directory has a different owner: {self.paths.runtime_dir}')
                for mode in ('none', 'sysbox'):
                    try:
                        os.unlink(f'compose.{mode}.json', dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(descriptor)
        # Retain recovery information until resource and file cleanup succeed.
        # Never remove application data or pass --volumes to Docker Compose.
        self.state.clear_mode()
        self.state.clear_pending()

    def exec(self, service: str, command: Sequence[str], *, workdir: Path,
             env_names: Sequence[str], interactive: bool, tty: bool,
             detach: bool = False, capture_output: bool = False) -> int:
        argv = docker_exec_argv(self.container_id(service), command, workdir=workdir,
                                env_names=env_names, interactive=interactive,
                                tty=tty, detach=detach)
        # No env mapping: Docker reads selected names from the current launcher
        # environment. Values never enter arguments or Compose configuration.
        return self.runner.run(argv, check=False, capture_output=capture_output).returncode

    def build(self, service: str, *, pull: bool, no_cache: bool) -> None:
        self._service(service)
        args = ['build']
        if pull:
            args.append('--pull')
        if no_cache:
            args.append('--no-cache')
        self._run(*args, service, runtime_mounts=False)

    def pull(self) -> None:
        if self.config.docker_mode == 'sysbox':
            self._run('pull', 'inner-docker', runtime_mounts=False)
