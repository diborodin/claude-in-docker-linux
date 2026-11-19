#!/bin/sh
# Read the native candidate or download exactly one verified Debian package.
set -eu
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

case "$1" in
    resolve|download) ;;
    *) fail 'Expected Claude Desktop apt operation: resolve or download' ;;
esac

arch="$(dpkg --print-architecture)"
case "$arch" in
    amd64|arm64) ;;
    *) fail "Unsupported Claude Desktop package architecture: $arch" ;;
esac
package="claude-desktop:$arch"

validate_version() {
    case "$1" in [0-9]*) ;; *) return 1 ;; esac
    [ "${#1}" -lt 128 ] && dpkg --validate-version "$1" >/dev/null 2>&1
}

if [ "$1" = download ]; then
    [ "$(wc -c < "$2")" -le 128 ] || fail 'Claude Desktop version file is too large'
    if grep -aq '[^[:space:]0-9A-Za-z.+~:-]' "$2"; then
        fail 'Invalid Claude Desktop version file'
    fi
    version="$(sed -n 's/^[[:space:]]*//;s/[[:space:]]*$//;/./p' "$2")"
    if [ "$version" != latest ]; then
        validate_version "$version" || fail 'Expected latest or an exact Claude Desktop Debian package version'
    fi
else
    version=latest
fi

# Use a fresh index with only Anthropic's source and restrict apt to the
# fingerprint published in Anthropic's "Install Claude Desktop" help article.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 0755 "$work"
mkdir "$work/lists"
install -d -o _apt -m 0755 "$work/key"
key="$work/key/anthropic.asc"
cat > "$work/https.conf" <<'EOF'
Acquire::https::CaInfo "/etc/ssl/certs/ca-certificates.crt";
Acquire::https::Timeout "20";
Acquire::Retries "1";
Acquire::Languages "none";
EOF
printf '%s\n' 'Fetching the Anthropic apt signing key.' >&2
/usr/lib/apt/apt-helper -c "$work/https.conf" -o Acquire::https::AllowRedirect=false \
    download-file https://downloads.claude.ai/claude-desktop/key.asc "$key" >&2
chmod 0644 "$key"
cat > "$work/desktop.sources" <<EOF
Types: deb
URIs: https://downloads.claude.ai/claude-desktop/apt/stable
Suites: stable
Components: main
Architectures: $arch
Signed-By: $key 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
EOF

apt_desktop() {
    command="$1"
    shift
    "$command" -c "$work/https.conf" \
        -o "Dir::Etc::SourceList=$work/desktop.sources" -o Dir::Etc::SourceParts=- \
        -o "Dir::State::lists=$work/lists" "$@"
}
apt_desktop apt-get -o APT::Update::Error-Mode=any update >&2
if [ "$version" = latest ]; then
    version="$(apt_desktop apt-cache policy "$package" | awk '$1 == "Candidate:" && NF == 2 { print $2 }')"
    validate_version "$version" || fail "No signed Claude Desktop apt candidate for $arch"
fi
if [ "$1" = resolve ]; then
    printf '%s\n' "$version"
    exit
fi

mkdir "$work/download"
chown _apt "$work/download"
(
    cd "$work/download"
    # Apt authenticates the metadata and checks the .deb hash against it.
    apt_desktop apt-get download "$package=$version" \
        || fail "Cannot download signed Claude Desktop $version for $arch. Try build --update-desktop."
    set -- ./*.deb
    if [ "$#" -ne 1 ] ||
        [ "$(dpkg-deb -f "$1" Package)" != claude-desktop ] ||
        [ "$(dpkg-deb -f "$1" Version)" != "$version" ] ||
        [ "$(dpkg-deb -f "$1" Architecture)" != "$arch" ]; then
        fail 'Downloaded Claude Desktop package failed its identity check'
    fi
    mkdir /out
    mv "$1" /out/claude-desktop.deb
)
