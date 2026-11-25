# Claude Code with Remote Control, for looking after the upload server from the Claude app
# (README, "How changes reach the server"). Runs as a non-root user; what it may touch is decided by
# the mounts in compose.yaml (full) or compose.request-only.yaml (narrow).
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tmux openssh-client python3 jq ripgrep procps less nano \
    && rm -rf /var/lib/apt/lists/*
# The docker CLI with compose and buildx (only useful with the Docker socket mounted).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

ARG UID=1500
ARG GID=100
RUN useradd --uid "$UID" --gid "$GID" --no-create-home --home-dir /home/claude --shell /bin/bash claude
COPY entrypoint.sh rc-loop.sh /usr/local/bin/

USER claude
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
