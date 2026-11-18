FROM jdxcode/mise:2026.11.5 AS mise

FROM ubuntu:26.04 AS claude-code

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
COPY image/claude-code.version /tmp/claude-code.version
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) platform=linux-x64 ;; \
      arm64) platform=linux-arm64 ;; \
      *) printf 'Unsupported Claude Code architecture: %s\n' "$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    read_version() { \
      jq -Rres --arg allowed "$1" \
        'select(length <= 128) | capture("\\A[\u0009-\u000d ]*(?<version>" + $allowed + ")[\u0009-\u000d ]*\\z") | .version | select(length < 128)' \
        /tmp/claude-code.version; \
    }; \
    numeric='[0-9]+\.[0-9]+\.[0-9]+'; \
    version="$(read_version "latest|$numeric")" \
      || { echo 'Invalid Claude Code version file; expected latest or numeric X.Y.Z' >&2; exit 1; }; \
    releases='https://downloads.claude.ai/claude-code-releases'; \
    if [ "$version" = latest ]; then \
      curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 10 --max-filesize 128 \
        "$releases/latest" -o /tmp/claude-code.version; \
      version="$(read_version "$numeric")" \
        || { echo 'Invalid latest Claude Code release; expected numeric X.Y.Z' >&2; exit 1; }; \
    fi; \
    release="$releases/$version"; \
    curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 60 \
      "$release/manifest.json" -o /tmp/claude-code-manifest.json; \
    checksum="$(jq -ers --arg platform "$platform" --arg version "$version" \
      'select(length == 1) | .[0] | select(.version == $version) | .platforms[$platform].checksum | select(type == "string" and test("\\A[a-f0-9]{64}\\z"))' \
      /tmp/claude-code-manifest.json)" \
      || { echo 'Invalid Claude Code release manifest or checksum' >&2; exit 1; }; \
    mkdir /out; \
    curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 600 \
      "$release/$platform/claude" -o /out/claude; \
    printf '%s  %s\n' "$checksum" /out/claude | sha256sum --check --strict; \
    chmod 0555 /out/claude; \
    rm /tmp/claude-code.version /tmp/claude-code-manifest.json

FROM ubuntu:26.04 AS common

ARG CLAUDE_USER=claude
ARG CLAUDE_UID=1000
ARG CLAUDE_GID=1000
ARG CLAUDE_HOME=/home/claude
ARG CLAUDE_HOST_UID=1000

ENV LANG=C.UTF-8 \
    MISE_CONFIG_DIR=/opt/user-tools \
    MISE_LOCKED=1 \
    MISE_LOCKED_REQUIRE_CHECKSUM=1

# Bootstrap packages only: graphical dependencies belong to desktop-runtime.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSLo /etc/apt/keyrings/docker.asc https://download.docker.com/linux/ubuntu/gpg \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n' \
      "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

# Ubuntu ships an unused UID-1000 account. Remove it before mirroring the host.
# Keep all build-argument values as quoted command arguments, never shell code.
RUN set -eu; \
    printf '%s\n' "$CLAUDE_USER" | grep -Eq '^[a-z_][a-z0-9_-]*$'; \
    case "$CLAUDE_UID:$CLAUDE_GID:$CLAUDE_HOST_UID" in *[!0-9:]*|:*|*::*|*:) exit 1 ;; esac; \
    test "$CLAUDE_UID" -gt 0; \
    test "$CLAUDE_GID" -gt 0; \
    case "$CLAUDE_HOME" in /*) ;; *) exit 1 ;; esac; \
    test "$CLAUDE_HOME" != /; \
    if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi; \
    if ! getent group "$CLAUDE_GID" >/dev/null; then \
        groupadd --gid "$CLAUDE_GID" -- "$CLAUDE_USER"; \
    fi; \
    useradd --create-home --uid "$CLAUDE_UID" --gid "$CLAUDE_GID" \
      --home-dir "$CLAUDE_HOME" --shell /bin/bash -- "$CLAUDE_USER"

ENV HOME="${CLAUDE_HOME}" \
    CLAUDE_CONFIG_DIR="${CLAUDE_HOME}/.claude" \
    PATH="/usr/local/bin:/usr/local/share/mise/shims:${CLAUDE_HOME}/.local/bin:${PATH}"

USER ${CLAUDE_USER}
WORKDIR ${CLAUDE_HOME}

FROM common AS cli

USER root

# Keep this sequence identical in both products. System paths survive home mounts.
COPY image/mise.toml /etc/mise/config.toml
COPY image/mise.lock /etc/mise/mise.lock
COPY image/mise.toml image/mise-user.* /opt/user-tools/
COPY image/customize.sh image/customize-user.* /usr/local/lib/claude-image/
RUN set -eu; \
    if [ -f /opt/user-tools/mise-user.toml ]; then \
      mv /opt/user-tools/mise-user.toml /opt/user-tools/config.toml; \
    else printf '%s\n' '[tools]' > /opt/user-tools/config.toml; fi; \
    if [ -f /opt/user-tools/mise-user.lock ]; then mv /opt/user-tools/mise-user.lock /opt/user-tools/mise.lock; fi; \
    rm /opt/user-tools/mise.toml; \
    chmod -R a+rX /opt/user-tools /etc/mise; \
    export HOME=/root MISE_YES=1; \
    mise bootstrap packages apply --manager apt \
 && mise install --locked --system \
 && mise reshim --system \
 && /usr/local/lib/claude-image/customize.sh \
 && if [ -f /usr/local/lib/claude-image/customize-user.sh ]; then \
      bash -euo pipefail /usr/local/lib/claude-image/customize-user.sh; \
    fi \
 && rm -f /usr/local/lib/claude-image/customize-user.sh \
 && rm -rf /var/lib/apt/lists/* /root/.cache/mise

USER ${CLAUDE_USER}

LABEL io.github.diborodin.claude-in-docker-linux.managed="true" \
      io.github.diborodin.claude-in-docker-linux.owner-uid="${CLAUDE_HOST_UID}"

CMD ["sleep", "infinity"]

ENV DISABLE_UPDATES=1
COPY --from=claude-code --chmod=0555 /out/claude /usr/local/bin/claude

FROM common AS desktop-runtime

USER root

# Anthropic's signed official repository, plus the X11/Electron runtime only.
RUN curl -fsSLo /usr/share/keyrings/claude-desktop-archive-keyring.asc \
      https://downloads.claude.ai/claude-desktop/key.asc \
 && printf '%s\n' 'deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] https://downloads.claude.ai/claude-desktop/apt/stable stable main' \
      > /etc/apt/sources.list.d/claude-desktop.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      claude-desktop openbox fonts-dejavu-core procps \
      libasound2t64 libatk-bridge2.0-0t64 libgtk-3-0t64 libnss3 \
      libx11-xcb1 libgbm1 libxss1 libxtst6 libxkbcommon0 libxshmfence1 \
 && rm -rf /var/lib/apt/lists/*

# Every Claude window fills the nested X server without window decorations.
RUN sed -i 's#</applications>#<application class="*"><decor>no</decor><maximized>yes</maximized></application></applications>#' \
      /etc/xdg/openbox/rc.xml \
 && grep -Fq '<application class="*"><decor>no</decor><maximized>yes</maximized></application>' \
      /etc/xdg/openbox/rc.xml

FROM desktop-runtime AS desktop

USER root

# Keep this sequence identical in both products. System paths survive home mounts.
COPY image/mise.toml /etc/mise/config.toml
COPY image/mise.lock /etc/mise/mise.lock
COPY image/mise.toml image/mise-user.* /opt/user-tools/
COPY image/customize.sh image/customize-user.* /usr/local/lib/claude-image/
RUN set -eu; \
    if [ -f /opt/user-tools/mise-user.toml ]; then \
      mv /opt/user-tools/mise-user.toml /opt/user-tools/config.toml; \
    else printf '%s\n' '[tools]' > /opt/user-tools/config.toml; fi; \
    if [ -f /opt/user-tools/mise-user.lock ]; then mv /opt/user-tools/mise-user.lock /opt/user-tools/mise.lock; fi; \
    rm /opt/user-tools/mise.toml; \
    chmod -R a+rX /opt/user-tools /etc/mise; \
    export HOME=/root MISE_YES=1; \
    mise bootstrap packages apply --manager apt \
 && mise install --locked --system \
 && mise reshim --system \
 && /usr/local/lib/claude-image/customize.sh \
 && if [ -f /usr/local/lib/claude-image/customize-user.sh ]; then \
      bash -euo pipefail /usr/local/lib/claude-image/customize-user.sh; \
    fi \
 && rm -f /usr/local/lib/claude-image/customize-user.sh \
 && rm -rf /var/lib/apt/lists/* /root/.cache/mise

USER ${CLAUDE_USER}

LABEL io.github.diborodin.claude-in-docker-linux.managed="true" \
      io.github.diborodin.claude-in-docker-linux.owner-uid="${CLAUDE_HOST_UID}"

CMD ["sleep", "infinity"]

ENV DISABLE_UPDATES=1
COPY --from=claude-code --chmod=0555 /out/claude /usr/local/bin/claude
