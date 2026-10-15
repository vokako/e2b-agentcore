FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

ENV COREPACK_HOME=/usr/local/lib/corepack
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.tuna.tsinghua.edu.cn"
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
ENV PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true

# amd64 images use archive/security.ubuntu.com; arm64 images use ports.ubuntu.com/ubuntu-ports
RUN f=/etc/apt/sources.list.d/ubuntu.sources; [ -f "$f" ] || f=/etc/apt/sources.list; \
    sed -i -e 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' \
           -e 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' \
           -e 's|http://ports.ubuntu.com|http://mirrors.aliyun.com|g' "$f"

ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg wget unzip git vim \
    && rm -rf /var/lib/apt/lists/*

# ---- AgentCore Runtime contract: HTTP server on 8080 (/ping, /invocations) ----
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY server.py /opt/agentcore/server.py
EXPOSE 8080
CMD ["python3", "-u", "/opt/agentcore/server.py"]
