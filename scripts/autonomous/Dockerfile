FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates coreutils curl git iproute2 iptables iputils-ping procps tini util-linux \
    build-essential pkg-config && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/p2r-venv
# Experiments install packages themselves: point pip at a mirror that is reachable from mainland China.
RUN printf '[global]\nindex-url = https://mirrors.aliyun.com/pypi/simple/\ntrusted-host = mirrors.aliyun.com\n' > /etc/pip.conf
RUN useradd --create-home --uid 10001 --shell /bin/bash agent
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["sleep","infinity"]
