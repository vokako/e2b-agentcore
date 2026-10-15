#!/bin/bash
set -o pipefail
while [ ! -f /var/log/podman-ready ]; do sleep 5; done
cd /root/b
uname -m; podman --version; /usr/local/bin/aws --version
REG=${ECR_REGISTRY:?set ECR_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com}
REPO=$REG/sandbox-poc/ubuntu24-base
for v in orig fixed; do
  df=Dockerfile; [ $v = fixed ] && df=Dockerfile.fixed
  echo "===== BUILD $v ====="
  s=$(date +%s)
  podman build --platform linux/arm64 -f $df -t $REPO:$v-arm64 . > build-$v.log 2>&1; rc=$?
  echo "rc=$rc secs=$(( $(date +%s)-s ))"
  grep -E '^(Get:|Hit:|Err:|Ign:)' build-$v.log | awk '{print $1, $2}' | sort | uniq -c | head -8
  tail -3 build-$v.log
  podman run --rm $REPO:$v-arm64 sh -c 'uname -m; grep ^URIs /etc/apt/sources.list.d/ubuntu.sources; git --version; curl --version | head -1; vim --version | head -1'
done
podman images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep ubuntu24
echo "===== PUSH ====="
/usr/local/bin/aws ecr get-login-password --region us-east-1 | podman login --username AWS --password-stdin $REG
for v in orig fixed; do podman push $REPO:$v-arm64 2>&1 | tail -2; done
podman manifest inspect docker://$REPO:fixed-arm64 2>/dev/null | head -20 || skopeo inspect docker://$REPO:fixed-arm64
