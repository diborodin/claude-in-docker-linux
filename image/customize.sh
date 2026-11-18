#!/usr/bin/env bash
set -euo pipefail

# Advanced customization hook: runs as root in both final image branches,
# after declarative packages, locked tools, and system shims are installed.
# Prefer ~/.config/claude-docker/mise.toml on the host for routine tool additions.
# Edit this advanced hook only in a source checkout. No commands by default.
