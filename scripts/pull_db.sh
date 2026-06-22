#!/usr/bin/env bash
#
# pull_db.sh — copy the live storage.db from the VPS down to this machine.
#
# The collectors/engine write to storage.db continuously, so a plain `scp` of
# the file can capture a torn state (a half-written page or an unmerged WAL).
# To avoid that we ask the VPS to make a *consistent* snapshot first, using
# SQLite's online-backup API (`VACUUM INTO`), then download that snapshot.
#
# Config — set these in the environment or in a gitignored scripts/.pull-db.env:
#   VPS_HOST   ssh target, e.g. "user@1.2.3.4" or an ~/.ssh/config alias  (required)
#   VPS_PATH   abs path to the repo on the VPS                            (required)
#   LOCAL_OUT  where to write the copy locally   (default: ./storage-vps.db)
#   VPS_PORT   ssh port, e.g. "51002"                                   (optional)
#
# Usage:
#   scripts/pull_db.sh                      # uses env / scripts/.pull-db.env
#   VPS_HOST=me@host VPS_PATH=~/crypto-signal-bot scripts/pull_db.sh
#   scripts/pull_db.sh me@host ~/crypto-signal-bot ./snap.db   # positional args

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional config file (gitignored). Lets you avoid retyping host/path.
ENV_FILE="${SCRIPT_DIR}/.pull-db.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

# Positional args override env, for one-off use.
VPS_HOST="${1:-${VPS_HOST:-}}"
VPS_PATH="${2:-${VPS_PATH:-}}"
LOCAL_OUT="${3:-${LOCAL_OUT:-./storage-vps.db}}"
VPS_PORT="${VPS_PORT:-}"

if [[ -z "$VPS_HOST" || -z "$VPS_PATH" ]]; then
  cat >&2 <<EOF
error: VPS_HOST and VPS_PATH are required.

Set them inline:
  VPS_HOST=user@host VPS_PATH=/home/user/crypto-signal-bot $0

or pass positionally:
  $0 user@host /home/user/crypto-signal-bot [local-out.db]

or create ${ENV_FILE} (gitignored) with:
  VPS_HOST=user@host
  VPS_PATH=/home/user/crypto-signal-bot
EOF
  exit 1
fi

# Remote paths. Snapshot lives next to the DB in the repo dir.
REMOTE_DB="${VPS_PATH%/}/storage.db"
REMOTE_SNAP="${VPS_PATH%/}/storage.snapshot.db"

echo ">> Snapshotting ${VPS_HOST}:${REMOTE_DB}"
ssh -p "$VPS_PORT" "$VPS_HOST" cp "$REMOTE_DB" "$REMOTE_SNAP"

echo ">> Downloading to ${LOCAL_OUT}"
mkdir -p "$(dirname "$LOCAL_OUT")"

scp -P "$VPS_PORT" "${VPS_HOST}:${REMOTE_SNAP}" "$LOCAL_OUT"

echo ">> Cleaning up remote snapshot"
ssh -p "$VPS_PORT" "$VPS_HOST" rm -f "$REMOTE_SNAP"

echo ">> Done: $(du -h "$LOCAL_OUT" | cut -f1) -> ${LOCAL_OUT}"
