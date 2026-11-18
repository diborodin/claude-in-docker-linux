"""Command-line parser; help and configuration init never initialize Docker."""

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import pwd
import subprocess
import sys

from . import __version__
from .checks import cleanup_inner_docker, lock_tools, run_checks
from .commands import CommandRunner
from .compose import ComposeProject
from .config import AccountInfo, load_config, resolve_config_file, write_example_config
from .environment import select_environment_names
from .desktop import launch_desktop, stop_desktop_runtime
from .image_tools import image_build_inputs
from .mounts import container_workdir, prepare_mounts
from .paths import resolve_host_paths, resolve_program_dir


def _report_config_source(path: Path, found: bool) -> None:
    suffix = '' if found else ' (not found; using defaults and environment overrides)'
    print(f'claude-docker: config: {path}{suffix}', file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ['_clipboard-worker']:
        from .clipboard import worker_main
        return worker_main()
    parser = argparse.ArgumentParser(
        prog="claude-docker", description="Run Claude Code and Desktop in Docker on Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    command_parsers = []

    def command(group, name, summary, *, details='', examples=(), **kwargs):
        epilog = 'Examples:\n' + '\n'.join('  ' + example for example in examples)
        child = group.add_parser(
            name, help=summary, description=summary + ('\n\n' + details if details else ''),
            epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs)
        command_parsers.append(child)
        return child

    config = command(commands, 'config', 'Manage launcher configuration.',
                     examples=('claude-docker config init', 'claude-docker config init --help'))
    config_commands = config.add_subparsers(dest='config_command', required=True)
    command(config_commands, 'init', 'Create private example configuration without overwriting.',
            details='Default path: ~/.config/claude-docker/config.toml.\n'
                    'XDG_CONFIG_HOME and CLAUDE_CONFIG_FILE can change this path.\n'
                    'Edit the example workspace roots before starting Claude.',
            examples=('claude-docker config init',))
    check = command(commands, 'check', 'Check host, Docker, and configured prerequisites.',
                    details='Read-only by default. --smoke creates and removes a temporary container.\n'
                            'Desktop checks describe the requested policy for the next start;\n'
                            'they do not verify a running Desktop sandbox.',
                    examples=('claude-docker check', 'claude-docker check --desktop',
                              'claude-docker check --desktop --clipboard', 'claude-docker check --smoke'))
    check.add_argument('--smoke', action='store_true', help='Run a temporary container after prerequisite checks (changes Docker state).')
    check.add_argument('--desktop', action='store_true', help='Check display, Xephyr, Xauthority, UID, sandbox and seccomp prerequisites.')
    check.add_argument('--clipboard', action='store_true', help='Also check Desktop and xclip for manual clipboard transfers.')
    check.add_argument('--clipboard-sync', action='store_true', help='Also check Desktop and bidirectional clipboard prerequisites (does not start sync).')
    check.add_argument('--no-sandbox', action='store_true', help='Check the unsafe sandbox-disabled Desktop policy (does not launch Desktop).')
    tools = command(commands, 'tools', 'Maintain pinned image tools.',
                    examples=('claude-docker tools lock', 'claude-docker tools lock --help'))
    tools_commands = tools.add_subparsers(dest='tools_command', required=True)
    command(tools_commands, 'init', 'Create a private optional user tool profile without overwriting.',
            details='Default: ~/.config/claude-docker/mise.toml. XDG_CONFIG_HOME and\n'
                    'CLAUDE_MISE_CONFIG_FILE select another path, independently of launcher config.\n'
                    'The sibling .lock uses the same stem. Both files survive updates, uninstall and purge.',
            examples=('claude-docker tools init',))
    tool_lock = command(tools_commands, 'lock', 'Lock user image tools through the pinned mise Docker image.',
            details='Works in installed bundles and source/development checkouts.\n'
                    'Preserves matching resolutions; --bump explicitly advances requests.\n'
                    'Uses Docker, not host mise; locks linux-x64 and linux-arm64.\n'
                    'An apt-only profile needs no artifact lock; apt packages are not artifact-pinned.\n'
                    'Build never creates or refreshes locks. Run tools lock before build.',
            examples=('claude-docker tools lock', 'claude-docker tools lock --bump',
                      './claude-docker tools lock --base'))
    tool_lock.add_argument('--base', action='store_true', help='Lock shipped baseline tools (source/development only).')
    tool_lock.add_argument('--bump', action='store_true', help='Advance matching version requests explicitly.')
    customize = command(commands, 'customize', 'Set up a personal image build script.',
                        examples=('claude-docker customize init',))
    customize_commands = customize.add_subparsers(dest='customize_command', required=True)
    command(customize_commands, 'init', 'Create a private example build script without overwriting.',
            details='Default: ~/.config/claude-docker/customize.sh. Respects XDG_CONFIG_HOME.\n'
                    'The example installs checkbashisms on the next image build.\n'
                    'The script runs as root during either image build. Move it aside to disable it.\n'
                    'It survives updates, uninstall, and purge.',
            examples=('claude-docker customize init',))
    cleanup = command(commands, 'cleanup', 'Explicitly delete named application resources.',
                      examples=('claude-docker cleanup inner-docker --help',))
    cleanup_commands = cleanup.add_subparsers(dest='cleanup_command', required=True)
    inner = command(cleanup_commands, 'inner-docker', 'Delete only your managed inner Docker data volume.',
                    details='This is permanent data deletion. Stop the containers first.\n'
                            'Shows an inventory and requires confirmation; --yes skips the prompt.',
                    examples=('claude-docker stop', 'claude-docker cleanup inner-docker',
                              'claude-docker cleanup inner-docker --yes'))
    inner.add_argument('--yes', action='store_true', help='Confirm permanent deletion without an interactive prompt.')
    for name, summary, details, examples in (
        ('build', 'Build the CLI image using the build cache.',
         'Default: CLI image only. --desktop and --all are mutually exclusive.\n'
         'The first build saves the latest Claude Code version. Later builds use the saved version.\n'
         'Runs your optional customize.sh as root before Claude Code is copied.\n'
         'Stop existing containers after building to use the new image.',
         ('claude-docker build', 'claude-docker build --desktop', 'claude-docker build --all --update-claude')),
        ('rebuild', 'Build the CLI image with fresh base images and no cache.',
         'Default: CLI image only. --desktop and --all are mutually exclusive.\n'
         'The first build saves the latest Claude Code version. Later builds use the saved version.\n'
         'Runs your optional customize.sh as root before Claude Code is copied.\n'
         'Stop existing containers after rebuilding to use the new image.',
         ('claude-docker rebuild', 'claude-docker rebuild --desktop', 'claude-docker rebuild --all --update-claude')),
        ('pull', 'Pull external images used by the selected Docker mode.',
         'Used for optional inner Docker in Sysbox mode. Does not build Claude images.\n'
         'In docker_mode=none there are no external images to pull.',
         ('claude-docker pull',)),
        ('stop', 'Stop and remove Claude containers and recorded Desktop display services.',
         'Retains host-mounted Claude state, workspaces, and named inner Docker data volumes.',
         ('claude-docker stop',)),
    ):
        child = command(commands, name, summary, details=details, examples=examples)
        if name in ('build', 'rebuild'):
            targets = child.add_mutually_exclusive_group()
            targets.add_argument('--desktop', action='store_true', help='Build only the Desktop image.')
            targets.add_argument('--all', action='store_true', help='Build both CLI and Desktop images.')
            child.add_argument('--update-claude', action='store_true',
                               help='Check the latest Claude Code release and save the selected version before building.')
    desktop = command(commands, 'desktop', 'Launch Claude Desktop in a private nested display.',
                      details='Put launcher options before application arguments. Use -- to separate them.\n'
                              'The default policy enables the sandbox and uses manual clipboard transfers.\n'
                              'A running process keeps its settings and credentials until restart or stop.\n'
                              'Sysbox mode starts inner Docker as needed and waits for it before launch or reuse.\n'
                              'Select seccomp in configuration with [desktop].seccomp or CLAUDE_DESKTOP_SECCOMP.',
                      examples=('claude-docker desktop', 'claude-docker desktop --restart',
                                'claude-docker desktop -- --help'),
                      usage='%(prog)s [--restart] [--clipboard-sync] [--no-sandbox] [-- APP_ARGS ...]')
    desktop.add_argument('--restart', action='store_true', help='Recreate only Desktop to apply current settings and refresh credentials.')
    desktop.add_argument('--clipboard-sync', action='store_true', help='Opt into bidirectional host/Desktop clipboard transfers (may transfer secrets).')
    desktop.add_argument('--no-sandbox', action='store_true', help='Disable the Chromium sandbox (unsafe; incompatible with chromium seccomp).')
    clipboard = command(commands, 'clipboard', 'Explicitly transfer the host/Desktop clipboard.',
                        details='Requires a live recorded Desktop session, host DISPLAY, and xclip.',
                        examples=('claude-docker clipboard push', 'claude-docker clipboard pull'))
    clipboard_commands = clipboard.add_subparsers(dest='clipboard_command', required=True)
    command(clipboard_commands, 'push', 'Copy host clipboard into Desktop (may include secrets).',
            examples=('claude-docker clipboard push',))
    command(clipboard_commands, 'pull', 'Copy Desktop clipboard onto the host.',
            details='Replaces the host clipboard with the Desktop clipboard contents.',
            examples=('claude-docker clipboard pull',))
    uninstall = command(commands, 'uninstall', 'Remove this installation while retaining user data and Docker storage.',
                        details='Normal removal retains launcher configuration, images, networks, and volumes.\n'
                                '--purge also removes eligible launcher configuration and managed Docker storage.\n'
                                'All modes retain workspaces and Claude state sources. Purge requires confirmation.',
                        examples=('claude-docker uninstall', 'claude-docker uninstall --purge',
                                  'claude-docker uninstall --purge --yes'))
    uninstall.add_argument('--purge', action='store_true', help='Also remove exact managed Docker resources and launcher configuration.')
    uninstall.add_argument('--yes', action='store_true', help='Confirm purge without an interactive prompt.')
    command(commands, 'claude', 'Run Claude Code with literal trailing application arguments.',
            details='All trailing arguments, including --help, go to Claude Code unchanged.',
            examples=('claude-docker claude', 'claude-docker claude --resume'),
            add_help=False, usage='%(prog)s [CLAUDE_ARGS ...]')
    # Generate the overview from the actual parsers so flags cannot drift from usage.
    overview = '\n'.join(
        '\n'.join('  ' + line for line in child.format_usage().removeprefix('usage: ').rstrip().splitlines())
        for child in command_parsers)
    parser.epilog = (
        'Command usage (put options after the command):\n' + overview + '\n\n'
        'For option descriptions, run: claude-docker COMMAND --help\n'
        'For example: claude-docker check --help\n\n'
        'Examples:\n'
        '  claude-docker config init\n'
        '  claude-docker build --all\n'
        '  claude-docker check --desktop --clipboard\n'
        '  claude-docker desktop --restart\n\n'
        'All arguments after claude go to Claude Code; claude --help runs application help.\n'
        'Use desktop -- --help for Desktop application help.')
    # Everything after the subcommand belongs to Claude, including --help and
    # leading options. argparse must never consume or reinterpret those tokens.
    claude_args = arguments[1:] if arguments[:1] == ['claude'] else []
    desktop_args = []
    if arguments[:1] == ['desktop']:
        boundary = 1
        while boundary < len(arguments) and arguments[boundary] in ('--restart', '--clipboard-sync', '--no-sandbox'):
            boundary += 1
        # A leading --help is launcher help; after an app argument it stays literal.
        if arguments[boundary:] == ['--help']:
            boundary += 1
        desktop_args = arguments[boundary:]
        if desktop_args[:1] == ['--']:
            desktop_args = desktop_args[1:]
        args = parser.parse_args(arguments[:boundary])
    else:
        args = parser.parse_args(arguments[:1] if arguments[:1] == ['claude'] else arguments)
    if args.command == 'uninstall':
        from .installer import InstallLayout
        from .uninstall import reject_shared_system, uninstall_user, print_report
        try:
            reject_shared_system(Path(__file__))
            layout = InstallLayout.user(os.environ)
            print_report(uninstall_user(layout, purge=args.purge, yes=args.yes))
        except subprocess.SubprocessError:
            sys.stderr.write('claude-docker: Docker resource discovery failed; no purge was started.\n')
            return 1
        except (ValueError, OSError) as error:
            sys.stderr.write(f'claude-docker: {error}\n')
            return 1
    elif args.command == "config" and args.config_command == "init":
        try:
            paths = resolve_host_paths(os.environ, os.getuid())
            destination = resolve_config_file(env=os.environ, paths=paths)
            write_example_config(destination)
        except (ValueError, OSError) as error:
            sys.stderr.write(f"claude-docker: {error}\n")
            return 1
        print(f"Created configuration: {destination}")
    elif args.command == 'tools' and args.tools_command == 'init':
        from .tool_config import init_user_tools
        try:
            paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
            destination = init_user_tools(paths, os.environ)
        except (ValueError, OSError) as error:
            sys.stderr.write(f'claude-docker: {error}\n')
            return 1
        print(f'claude-docker: tools: {destination}', file=sys.stderr)
        print(f'Created tool profile: {destination}')
    elif args.command == 'customize' and args.customize_command == 'init':
        from .customize import init_user_customize
        try:
            paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
            program_dir = resolve_program_dir(Path(__file__).resolve().parents[2] / 'claude-docker', os.environ)
            destination = init_user_customize(paths, program_dir)
        except (ValueError, OSError) as error:
            sys.stderr.write(f'claude-docker: {error}\n')
            return 1
        print(f'Created customization script: {destination}')
    elif args.command == 'clipboard':
        from .clipboard import clipboard_push, clipboard_pull
        from .desktop import XephyrManager
        try:
            paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
            runtime = XephyrManager(paths).load()
            if runtime is None:
                raise ValueError('Clipboard requires a live recorded Desktop runtime')
            operation = clipboard_push if args.clipboard_command == 'push' else clipboard_pull
            operation(runtime, host_display=os.environ.get('DISPLAY', ''))
        except (ValueError, OSError):
            sys.stderr.write('claude-docker: Clipboard operation failed; verify the live Desktop runtime, host DISPLAY and xclip.\n')
            return 1
    elif args.command in ('check', 'tools', 'cleanup'):
        try:
            uid = os.getuid()
            paths = resolve_host_paths(os.environ, uid, create_runtime=False)
            account = pwd.getpwuid(uid)
            config = load_config(env=os.environ, paths=paths,
                                 account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)),
                                 report_source=_report_config_source)
            runner = CommandRunner()
            if args.command == 'check':
                results = run_checks(config, smoke=args.smoke, runner=runner,
                                     desktop=args.desktop, clipboard=args.clipboard,
                                     clipboard_sync=args.clipboard_sync, no_sandbox=args.no_sandbox)
                for result in results:
                    print(f'{result.status}: {result.name}: {result.message}')
                return 1 if any(result.status == 'error' for result in results) else 0
            project = ComposeProject(config, paths, (), runner)
            if args.command == 'tools':
                lock_tools(project, base=args.base, bump=args.bump)
            else:
                cleanup_inner_docker(project, yes=args.yes)
        except (ValueError, OSError) as error:
            sys.stderr.write(f"claude-docker: {error}\n")
            return 1
        except subprocess.CalledProcessError as error:
            sys.stderr.write(f"claude-docker: Docker maintenance failed (exit {error.returncode})\n")
            return 1
    elif args.command == 'stop':
        try:
            uid = os.getuid()
            paths = resolve_host_paths(os.environ, uid, create_runtime=False)
            failures = []
            project = None
            runner = CommandRunner()
            try:
                account = pwd.getpwuid(uid)
                config = load_config(env=os.environ, paths=paths,
                                     account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)),
                                     report_source=_report_config_source)
                project = ComposeProject(config, paths, (), runner)
            except (ValueError, OSError, KeyError):
                failures.append('Configuration or Compose project setup failed')
                try:
                    project = ComposeProject.for_teardown(paths, runner)
                except (ValueError, OSError, KeyError):
                    failures.append('Docker Compose teardown setup failed')
            # Recorded host cleanup must run independently of configuration and
            # project construction; its component cleanup also continues on error.
            try:
                stop_desktop_runtime(paths, project=project)
            except (ValueError, OSError, subprocess.SubprocessError):
                failures.append('Desktop runtime cleanup failed')
            if project is not None:
                try:
                    project.down()
                except (ValueError, OSError, subprocess.SubprocessError):
                    failures.append('Docker Compose teardown failed')
            if failures:
                raise ValueError('; '.join(failures))
        except (ValueError, OSError) as error:
            sys.stderr.write(f"claude-docker: {error}\n")
            return 1
    elif args.command in ('build', 'rebuild', 'pull', 'claude', 'desktop'):
        try:
            uid = os.getuid()
            paths = resolve_host_paths(os.environ, uid, create_runtime=args.command == 'claude')
            account = pwd.getpwuid(uid)
            config = load_config(env=os.environ, paths=paths,
                                 account=AccountInfo(account.pw_name, uid, os.getgid(),
                                                     Path(account.pw_dir)),
                                 report_source=_report_config_source)
            if args.command == 'desktop':
                return launch_desktop(config, restart=args.restart, clipboard_sync=args.clipboard_sync,
                                      no_sandbox=args.no_sandbox, app_args=desktop_args)
            env_names = select_environment_names(config.environment, os.environ) if args.command == 'claude' else ()
            mounts = prepare_mounts(config) if args.command == 'claude' else ()
            project = ComposeProject(config, paths, mounts, CommandRunner())
            if args.command == 'claude':
                workdir = container_workdir(Path.cwd(), mounts, config.container.home)
                project.up('claude-cli')
                return project.exec('claude-cli', ['claude', *claude_args], workdir=workdir,
                                    env_names=env_names, interactive=True,
                                    tty=sys.stdin.isatty() and sys.stdout.isatty())
            elif args.command in ('build', 'rebuild'):
                rebuild = args.command == 'rebuild'
                services = (('claude-cli', 'claude-desktop') if args.all else
                            ('claude-desktop',) if args.desktop else ('claude-cli',))
                with image_build_inputs(project.program_dir, paths, os.environ,
                                        update_claude=args.update_claude) as context:
                    for service in services:
                        project.build(service, pull=rebuild, no_cache=rebuild, build_context=context)
            elif args.command == 'pull':
                project.pull()
                if config.docker_mode == 'none':
                    print('No external images to pull in none mode.')
        except (ValueError, OSError) as error:
            sys.stderr.write(f"claude-docker: {error}\n")
            return 1
        except subprocess.CalledProcessError as error:
            sys.stderr.write(f"claude-docker: Docker Compose failed (exit {error.returncode})\n")
            return 1
    return 0
