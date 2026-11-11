#!/usr/bin/env bash
# 上传构建产物：输出字节进度，连接中断时续传，完成后才允许部署。
set -Eeuo pipefail
remote=${1:?Remote directory required}
shift
[[ "$remote" =~ ^/tmp/evan[a-z-]*ci-[a-f0-9]{40}-[0-9]+-[0-9]+$ ]]
: "${HOST:?HOST required}"
key=${UPLOAD_SSH_KEY:-$HOME/.ssh/deploy}
export RSYNC_RSH="ssh -i $key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
# SSH 已提供传输完整性；--append-verify 会核验续传前缀与完整文件。
for attempt in 1 2 3; do
  echo "Upload attempt $attempt/3: $(du -ch "$@" | tail -1)"
  if timeout 600s rsync -rt --partial --append-verify --info=progress2 --outbuf=L --timeout=60 "$@" "root@$HOST:$remote/"; then
    echo UPLOAD_COMPLETE
    exit 0
  fi
  echo "Upload interrupted; retrying with the partial files retained on the server" >&2
  sleep 3
done
echo 'Upload failed after three attempts; deployment has not started' >&2
exit 1
