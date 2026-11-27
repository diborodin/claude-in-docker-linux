#!/usr/bin/env bash
set -euo pipefail

# Optional example: runs as root inside both image builds.
# Remove sections you do not need before running build.
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
cd -- "$work_dir"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends perl ca-certificates curl gnupg
apt-get download devscripts
dpkg-deb --fsys-tarfile devscripts_*.deb | tar -xO ./usr/bin/checkbashisms >checkbashisms
install -m 0755 checkbashisms /usr/local/bin/checkbashisms

# Google Cloud CLI: trust this key only for the Google Cloud apt repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 60 \
  https://packages.cloud.google.com/apt/doc/apt-key.gpg -o google-cloud.asc
gpg --batch --yes --dearmor -o /etc/apt/keyrings/google-cloud.gpg google-cloud.asc
chmod 0644 /etc/apt/keyrings/google-cloud.gpg
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main' \
  >/etc/apt/sources.list.d/google-cloud-sdk.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-cloud-cli
