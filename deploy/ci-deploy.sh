#!/usr/bin/env bash
# GitHub 构建产物部署入口。只更新 Agent，保留生产凭据和持久化数据。
set -Eeuo pipefail
incoming=${1:?Incoming directory required}
sha=${2:?Commit SHA required}
run=${3:?Run identifier required}
[[ "$sha" =~ ^[a-f0-9]{40}$ && "$run" =~ ^[0-9]+-[0-9]+$ ]]
[[ "$incoming" == "/tmp/evan-agent-ci-$sha-$run" ]]
exec 9>/var/lock/evanlink-release.lock
flock -w 600 9
cd /opt/evan-agent
umask 077
backup="backups/ci-$sha-$run"
mkdir -m 700 "$backup"
cp -p .env "$backup/image.env"
cp -p compose.yaml "$backup/compose.yaml"
test -f shared/agent.env
test -d shared/data
docker load -i "$incoming/agent.tar"
image="evan-agent:$sha"
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
  sed -i "s/^AGENT_IMAGE=.*/AGENT_IMAGE=$image/" .env
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
rm -- "$incoming/agent.tar"
