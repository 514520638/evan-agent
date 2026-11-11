#!/usr/bin/env bash
# [POS] 基于固定本地运行环境生成版本化代码镜像。
# [INPUT] CI 代码包、现有 Agent 镜像及其锁定依赖。
# [OUTPUT] evan-agent:SHA；依赖不符时终止，不操作运行中的服务。
set -Eeuo pipefail
incoming=${1:?Incoming directory required}
sha=${2:?Commit required}
[[ "$sha" =~ ^[a-f0-9]{40}$ ]]
stage="$incoming/code"
mkdir "$stage"
tar -xzf "$incoming/code.tar.gz" -C "$stage"
expected=$(sha256sum "$stage/uv.lock" "$stage/pyproject.toml" | awk '{print $1}')
fingerprint=$(printf '%s' "$expected" | sha256sum | cut -d' ' -f1)
runtime="evan-agent-runtime:$fingerprint"
if ! docker image inspect "$runtime" >/dev/null 2>&1; then
  # 首次从已运行的镜像引导，后续始终复用固定环境，避免层数不断增长。
  base=$(docker inspect --format '{{.Image}}' evan-agent-agent-1)
else
  base=$runtime
fi
actual=$(docker run --rm --network none --memory 64m --entrypoint sha256sum "$base" /app/uv.lock /app/pyproject.toml | awk '{print $1}')
if [[ "$actual" != "$expected" ]]; then
  echo 'Runtime dependencies differ. Provision a matching runtime image before publishing; current service was not changed.' >&2
  exit 1
fi
docker tag "$base" "$runtime"
echo "Using verified local runtime: $runtime"
# 本地固定标签且禁用拉取；仅删除旧源码、复制新源码，不执行 uv/pip。
DOCKER_BUILDKIT=0 timeout 180s docker build --pull=false --network=none \
  --build-arg "RUNTIME_IMAGE=$runtime" -f "$incoming/Dockerfile.code" \
  -t "evan-agent:$sha" "$stage"
docker run --rm --network none --memory 256m --entrypoint python "evan-agent:$sha" \
  -c 'import evan_agent.api; import evan_agent.main; print("Code image import check passed")'
