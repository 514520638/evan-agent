#!/usr/bin/env bash
# [POS] Agent 构建产物部署入口，保留生产凭据和持久化数据。
# [INPUT] .github/workflows/deploy.yml (POS: Agent 验证、镜像构建与传输流程)
#         compose.yaml (POS: Agent 生产容器运行配置)
# [OUTPUT] 拉取指定摘要的镜像、备份数据、切换版本并验证，失败恢复旧版本。
set -Eeuo pipefail
incoming=${1:?Incoming directory required}
sha=${2:?Commit SHA required}
run=${3:?Run identifier required}
[[ "$sha" =~ ^[a-f0-9]{40}$ && "$run" =~ ^[0-9]+-[0-9]+$ ]]
[[ "$incoming" == "/tmp/evan-agent-ci-$sha-$run" ]]
# 拉取成功后才获取发布锁、停止服务。拉取失败不影响当前容器。
image=$(cat "$incoming/image-ref.txt")
[[ "$image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]
echo "Pulling $image"
timeout 1200s docker pull "$image"
exec 9>/var/lock/evanlink-release.lock
echo "Waiting for server release lock (up to 10 minutes)"
flock -w 600 9
echo "Release lock acquired"
cd /opt/evan-agent
umask 077
backup="backups/ci-$sha-$run"
mkdir -m 700 "$backup"
cp -p .env "$backup/image.env"
cp -p compose.yaml "$backup/compose.yaml"
test -f shared/agent.env
test -d shared/data
docker image inspect "$image" >/dev/null
# 配置语法检查先于停机，避免失败部署中断服务。
AGENT_IMAGE="$image" docker compose -f "$incoming/compose.yaml" config --quiet
restore() {
  status=$?
  if (( status != 0 )); then
    cp -p "$backup/image.env" .env
    cp -p "$backup/compose.yaml" compose.yaml
    docker compose up -d --no-deps agent || true
    echo "Agent deployment failed. Previous image restored; data snapshot: $backup/data.tar.gz" >&2
  fi
  exit "$status"
}
trap restore EXIT
docker compose stop agent
tar -czf "$backup/data.tar.gz" -C shared/data .
cp "$incoming/compose.yaml" compose.yaml
if grep -q '^AGENT_IMAGE=' .env; then
  sed -i "s|^AGENT_IMAGE=.*|AGENT_IMAGE=$image|" .env
else
  printf '\nAGENT_IMAGE=%s\n' "$image" >> .env
fi
docker compose up -d --no-deps agent
ready=0
for attempt in $(seq 1 45); do
  if curl -fsS --max-time 3 http://127.0.0.1:8001/agent-api/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" -eq 1
curl -fsS --max-time 15 https://evanlink.cn/agent-api/health
docker compose ps
echo "Agent deployed: $sha"
