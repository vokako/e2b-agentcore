FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

ENV COREPACK_HOME=/usr/local/lib/corepack
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.tuna.tsinghua.edu.cn"
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
ENV PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true

RUN sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g; s|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/ubuntu.sources ||     sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g; s|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list

ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update &&     apt-get install -y --no-install-recommends     curl     ca-certificates     gnupg     wget     unzip     git     vim
