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
from .mounts import container_workdir, prepare_mounts
from .paths import resolve_host_paths


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ['_clipboard-worker']:
        from .clipboard import worker_main
        return worker_main()
    parser = argparse.ArgumentParser(prog="claude-docker", description="Run Claude in Docker on Linux.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser("config", help="Manage launcher configuration.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("init", help="Create private example configuration without overwriting.")
    check = commands.add_parser('check', help='Check prerequisites without changing host or Docker state.')
    check.add_argument('--smoke', action='store_true', help='Explicitly run a temporary test container.')
    check.add_argument('--desktop', action='store_true', help='Check Desktop prerequisites.')
    check.add_argument('--clipboard', action='store_true', help='Check manual clipboard prerequisites.')
    check.add_argument('--clipboard-sync', action='store_true', help='Check bidirectional clipboard prerequisites.')
    check.add_argument('--no-sandbox', action='store_true', help='Report an unsafe Desktop sandbox override.')
    tools = commands.add_parser('tools', help='Maintain pinned image tools.')
    tools_commands = tools.add_subparsers(dest='tools_command', required=True)
    tools_commands.add_parser('lock', help='Regenerate image/mise.lock through the pinned mise Docker image.')
    cleanup = commands.add_parser('cleanup', help='Explicitly delete named application resources.')
    cleanup_commands = cleanup.add_subparsers(dest='cleanup_command', required=True)
    inner = cleanup_commands.add_parser('inner-docker', help='Delete only your managed inner Docker data volume.')
    inner.add_argument('--yes', action='store_true', help='Confirm permanent deletion without an interactive prompt.')
    for name, help_text in (
        ('build', 'Build the CLI image using the build cache.'),
        ('rebuild', 'Build the CLI image with fresh base images and no cache.'),
        ('pull', 'Pull external images used by the selected Docker mode.'),
        ('stop', 'Stop both Claude services without deleting application data.'),
    ):
        command_parser = commands.add_parser(name, help=help_text)
        if name in ('build', 'rebuild'):
            targets = command_parser.add_mutually_exclusive_group()
            targets.add_argument('--desktop', action='store_true', help='Build only the Desktop image.')
            targets.add_argument('--all', action='store_true', help='Build both CLI and Desktop images.')
    desktop = commands.add_parser('desktop', help='Launch Claude Desktop in a private nested display.')
    desktop.add_argument('--restart', action='store_true', help='Recreate only Desktop to refresh its environment.')
    desktop.add_argument('--clipboard-sync', action='store_true', help='Opt into bidirectional clipboard synchronization.')
    desktop.add_argument('--no-sandbox', action='store_true', help='Explicitly disable the Chromium sandbox (unsafe).')
    clipboard = commands.add_parser('clipboard', help='Explicitly transfer the host/Desktop clipboard.')
    clipboard_commands = clipboard.add_subparsers(dest='clipboard_command', required=True)
    clipboard_commands.add_parser('push', help='Copy host clipboard into Desktop (may include secrets).')
    clipboard_commands.add_parser('pull', help='Copy Desktop clipboard onto the host.')
    uninstall = commands.add_parser('uninstall', help='Remove this installation while retaining user data and Docker storage.')
    uninstall.add_argument('--purge', action='store_true', help='Also remove exact managed Docker resources and launcher configuration.')
    uninstall.add_argument('--yes', action='store_true', help='Confirm purge without an interactive prompt.')
    commands.add_parser('claude', help='Run Claude with literal trailing Claude arguments.', add_help=False)
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
                                 account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)))
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
                lock_tools(project)
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
                                     account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)))
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
                                                     Path(account.pw_dir)))
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
                for service in services:
                    project.build(service, pull=rebuild, no_cache=rebuild)
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
