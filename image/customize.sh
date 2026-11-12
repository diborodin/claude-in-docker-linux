#!/usr/bin/env bash
set -euo pipefail

# Advanced customization hook: runs as root in both final image branches,
# after declarative packages, locked tools, and system shims are installed.
# Prefer image/mise.toml for routine tool additions. No commands by default.
