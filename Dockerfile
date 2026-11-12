FROM jdxcode/mise:2026.11.5 AS mise

FROM ubuntu:24.04 AS common

ARG CLAUDE_USER=claude
ARG CLAUDE_UID=1000
ARG CLAUDE_GID=1000
ARG CLAUDE_HOME=/home/claude
ARG CLAUDE_HOST_UID=1000

ENV LANG=C.UTF-8

# Bootstrap packages only: graphical dependencies belong to desktop-runtime.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSLo /etc/apt/keyrings/docker.asc https://download.docker.com/linux/ubuntu/gpg \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n' \
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
RUN curl -fsSLo /tmp/claude-install.sh https://claude.ai/install.sh \
 && bash /tmp/claude-install.sh \
 && rm -f /tmp/claude-install.sh

# Preserve native Code outside a mounted home and ahead of host-local binaries.
USER root
RUN install -m 0555 "$(readlink -f "${CLAUDE_HOME}/.local/bin/claude")" /usr/local/bin/claude
USER ${CLAUDE_USER}

FROM common AS cli

USER root

# Keep this sequence identical in both products. System paths survive home mounts.
COPY image/mise.toml /etc/mise/config.toml
COPY image/mise.lock /etc/mise/mise.lock
COPY image/customize.sh /usr/local/lib/claude-image/customize.sh
RUN set -eu; \
    export HOME=/root MISE_YES=1; \
    mise bootstrap packages apply --manager apt \
 && mise install --locked --system \
 && mise reshim --system \
 && /usr/local/lib/claude-image/customize.sh \
 && rm -rf /var/lib/apt/lists/* /root/.cache/mise

USER ${CLAUDE_USER}

LABEL io.github.diborodin.claude-in-docker-linux.managed="true" \
      io.github.diborodin.claude-in-docker-linux.owner-uid="${CLAUDE_HOST_UID}"

CMD ["sleep", "infinity"]

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
COPY image/customize.sh /usr/local/lib/claude-image/customize.sh
RUN set -eu; \
    export HOME=/root MISE_YES=1; \
    mise bootstrap packages apply --manager apt \
 && mise install --locked --system \
 && mise reshim --system \
 && /usr/local/lib/claude-image/customize.sh \
 && rm -rf /var/lib/apt/lists/* /root/.cache/mise

USER ${CLAUDE_USER}

LABEL io.github.diborodin.claude-in-docker-linux.managed="true" \
      io.github.diborodin.claude-in-docker-linux.owner-uid="${CLAUDE_HOST_UID}"

CMD ["sleep", "infinity"]
