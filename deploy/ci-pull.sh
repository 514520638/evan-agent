#!/usr/bin/env bash
# [POS] 发布前拉取镜像，下载失败不会触发停机。
# [INPUT] ci-deploy.sh 提供的已固定摘要的仓库镜像引用。
# [OUTPUT] 验证本机镜像可用；失败明确报告网络/凭据/超时错误。
set -Eeuo pipefail
image=${1:?Image digest required}
[[ "$image" =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]
if docker image inspect "$image" >/dev/null 2>&1; then
  echo 'Image already available locally; skipping download'
  exit 0
fi
log=$(mktemp)
trap 'rm -f "$log"' EXIT
for attempt in 1 2; do
  echo "Pull attempt $attempt/2 (20 minute limit): $image"
  if timeout --kill-after=30s 1200s docker pull "$image" 2>&1 | tee "$log"; then
    docker image inspect "$image" >/dev/null
    exit 0
  else
    status=$?
  fi
  if grep -Eiq 'unauthorized|authentication required|denied|manifest unknown|name unknown' "$log"; then
    echo 'Registry access or image reference failed; check server docker login and package permissions' >&2
    exit "$status"
  fi
  echo "Pull failed (exit $status). Completed cached layers may be reused; incomplete layers may restart." >&2
  if (( attempt < 2 )); then sleep 5; fi
done
echo 'Image download failed before service update. For repeated EOF/timeouts, configure a registry reachable reliably from this server.' >&2
exit 1
