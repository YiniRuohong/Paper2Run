FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates coreutils curl git iproute2 iptables iputils-ping procps tini util-linux \
    build-essential pkg-config && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/p2r-venv
RUN useradd --create-home --uid 10001 --shell /bin/bash agent
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["sleep","infinity"]
