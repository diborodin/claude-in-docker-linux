#!/usr/bin/env bash
set -euo pipefail

# This script runs as root inside the image build.
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
cd -- "$work_dir"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends perl
apt-get download devscripts
dpkg-deb --fsys-tarfile devscripts_*.deb | tar -xO ./usr/bin/checkbashisms > checkbashisms
install -m 0755 checkbashisms /usr/local/bin/checkbashisms
