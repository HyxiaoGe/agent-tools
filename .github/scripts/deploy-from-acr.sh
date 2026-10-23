#!/usr/bin/env bash
# 用法：deploy-from-acr.sh <gateway|image|search|reader|trends>
# 从 ACR 拉本次提交在 Windows 上构建的镜像，用 compose 重建对应容器。
set -euo pipefail

service="${1:?缺少服务名}"
case "$service" in
  gateway) dir="$DEPLOY_DIR/gateway" ;;
  image | search | reader | trends) dir="$DEPLOY_DIR/services/$service" ;;
  *) echo "未知服务：$service" >&2; exit 1 ;;
esac
: "${REGISTRY:?}" "${IMAGE_PREFIX:?}" "${GITHUB_SHA:?}" "${ACR_USERNAME:?}" "${ACR_PASSWORD:?}"

repo="$IMAGE_PREFIX/agent-tools-$service"
ref="$repo:$GITHUB_SHA"

# 凭据放在本任务的临时目录，不写 ~/.docker，也不和 Fusion 部署的登录互相覆盖。
export DOCKER_CONFIG="$RUNNER_TEMP/.docker-$GITHUB_RUN_ID-$GITHUB_JOB"
mkdir -p "$DOCKER_CONFIG"
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

n=0
until printf '%s' "$ACR_PASSWORD" | docker login "$REGISTRY" -u "$ACR_USERNAME" --password-stdin; do
  n=$((n + 1))
  if [ "$n" -ge 5 ]; then echo "docker login 重试 $n 次仍失败"; exit 1; fi
  echo "docker login 失败，第 $n 次，10s 后重试..."
  sleep 10
done

n=0
until docker pull -q "$ref"; do
  n=$((n + 1))
  if [ "$n" -ge 3 ]; then echo "拉取 $ref 失败"; exit 1; fi
  sleep 10
done

# compose 默认用 agent-tools/<服务>:current；在 dev 上手动 docker compose up -d 时沿用当前部署的镜像，不会重新构建。
docker tag "$ref" "agent-tools/$service:current"
cd "$dir"
AGENT_TOOLS_IMAGE="$ref" docker compose up -d

# 本机只留本次部署的版本，历史版本在 ACR 里。
docker images "$repo" --format '{{.Repository}}:{{.Tag}}' | grep -vxF "$ref" | xargs -r docker rmi >/dev/null || true
docker image prune -f >/dev/null
