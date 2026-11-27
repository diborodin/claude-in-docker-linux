"""Utilities for isolating lifecycle locks in test processes."""

import sys
from types import SimpleNamespace
from unittest.mock import patch


def isolate_runtime_lock(test, account_home):
    account = patch('claude_docker.state.getpwuid', return_value=SimpleNamespace(pw_dir=str(account_home)))
    account.start()
    test.addCleanup(account.stop)


# Patch the account lookup before importing an installed bundle in a child.
# The launcher still executes its actual bootstrap with its original argv.
ACCOUNT_HOME_BOOTSTRAP = '''
import os
import pwd
import sys

account_home = sys.argv.pop(1)
account_uid = os.getuid()
getpwuid = pwd.getpwuid

def test_getpwuid(uid):
    entry = getpwuid(uid)
    if uid == account_uid:
        return pwd.struct_passwd((*entry[:5], account_home, entry.pw_shell))
    return entry

pwd.getpwuid = test_getpwuid
'''


def runtime_script_args(account_home, script, *arguments):
    bootstrap = ACCOUNT_HOME_BOOTSTRAP + '''
import runpy

sys.argv.pop(0)
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    return [sys.executable, '-c', bootstrap, str(account_home), str(script), *map(str, arguments)]
