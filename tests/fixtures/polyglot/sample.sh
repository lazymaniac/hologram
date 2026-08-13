#!/usr/bin/env bash
set -euo pipefail

_log() {
  echo "[deploy] $1" >&2
}

build_image() {
  local tag=$1
  docker build -t "$tag" .
  _log "built $tag"
}

function deploy {
  _log "deploying $1"
  build_image "$1"
  docker push "$1"
}

MAX_RETRIES=3
export REGISTRY="docker.example.io"
readonly DEPLOY_TOKEN="tok123"
STAMP=$(date +%s)
