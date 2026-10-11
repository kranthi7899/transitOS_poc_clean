#!/usr/bin/env bash
# Project-local single-node PostgreSQL for the Postgres profile (TRANSITOS_DB=postgres).
# Everything lives under <repo>/.local (gitignored); nothing is installed system-wide.
#
#   scripts/postgres.sh fetch     download the binary jar from Maven Central, verify SHA-256, extract
#   scripts/postgres.sh up        fetch if needed, initdb once, start, wait until ready, create the database
#   scripts/postgres.sh down      stop the server (fast shutdown) and confirm it is gone
#   scripts/postgres.sh status    pid, port, RSS
#   scripts/postgres.sh reset     down + delete the data dir (all schemas)
#   scripts/postgres.sh purge     drop every test/scenario schema (all but the canonical transitos_* ones)
#
# Binary: the zonky.io "embedded-postgres-binaries" build of upstream PostgreSQL (server only: postgres,
# initdb, pg_ctl; no psql). On Apple Silicon the published darwin build is x86_64 and runs under Rosetta.
# Env: PG_VERSION (default 17.7.0), TRANSITOS_PG_PORT (default 54329).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_VERSION="${PG_VERSION:-17.7.0}"
PORT="${TRANSITOS_PG_PORT:-54329}"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ART="darwin-arm64v8"; TXZ="postgres-darwin-arm_64.txz" ;;
  Darwin-x86_64) ART="darwin-amd64";   TXZ="postgres-darwin-x86_64.txz" ;;
  Linux-x86_64)  ART="linux-amd64";    TXZ="postgres-linux-x86_64.txz" ;;
  Linux-aarch64) ART="linux-arm64v8";  TXZ="postgres-linux-arm_64.txz" ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
JAR="embedded-postgres-binaries-$ART-$PG_VERSION.jar"
URL="https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-$ART/$PG_VERSION"
LOCAL="$ROOT/.local"
PDIR="$LOCAL/postgres"
PHOME="$PDIR/$PG_VERSION"
DATA="$LOCAL/postgres-data"
LOGFILE="$LOCAL/postgres-data.log"

log() { printf '[postgres] %s\n' "$*" >&2; }

fetch() {
  if [[ -x "$PHOME/bin/postgres" ]]; then return 0; fi
  local dl="$PDIR/download"
  rm -rf "$dl"; mkdir -p "$dl"
  log "downloading $JAR"
  curl -fsSL -o "$dl/$JAR" "$URL/$JAR" && curl -fsSL -o "$dl/$JAR.sha256" "$URL/$JAR.sha256" \
    || { log "download failed"; rm -rf "$dl"; exit 1; }
  local want got
  want="$(tr -d ' \t\r\n' < "$dl/$JAR.sha256")"
  got="$(shasum -a 256 "$dl/$JAR" | awk '{print $1}')"
  if [[ ${#want} -ne 64 || "$want" != "$got" ]]; then
    log "SHA-256 MISMATCH: published=$want computed=$got"; rm -rf "$dl"; exit 1
  fi
  log "SHA-256 verified: $got"
  mkdir -p "$dl/jar"; unzip -q -o "$dl/$JAR" -d "$dl/jar"
  mkdir -p "$PHOME"; tar -xJf "$dl/jar/$TXZ" -C "$PHOME"
  printf '%s  %s\n' "$got" "$JAR" > "$PDIR/$JAR.sha256.verified"
  rm -rf "$dl"
}

alive() { [[ -f "$DATA/postmaster.pid" ]] && kill -0 "$(head -1 "$DATA/postmaster.pid")" 2>/dev/null; }
port_open() { nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; }

write_conf() {
  cat >> "$DATA/postgresql.conf" <<CONF

# ---- transitos local profile (appended by scripts/postgres.sh) ----
listen_addresses = '127.0.0.1'
port = $PORT
unix_socket_directories = ''
max_connections = 300
shared_buffers = 64MB
max_locks_per_transaction = 256
log_min_messages = warning
CONF
}

up() {
  fetch
  if alive; then log "already running (pid $(head -1 "$DATA/postmaster.pid"))"; return 0; fi
  if port_open; then log "port $PORT is taken by another process; refusing to start"; exit 1; fi
  if [[ ! -f "$DATA/PG_VERSION" ]]; then
    log "initdb"
    "$PHOME/bin/initdb" -D "$DATA" -U transitos --auth=trust -E UTF8 --locale=C >&2
    write_conf
  fi
  local t0 t1
  t0="$(python3 -c 'import time;print(time.time())')"
  "$PHOME/bin/pg_ctl" -D "$DATA" -l "$LOGFILE" -w -t 60 start >&2 \
    || { log "start failed; tail of log:"; tail -30 "$LOGFILE" >&2; exit 1; }
  t1="$(python3 -c 'import time;print(time.time())')"
  log "ready in $(python3 -c "print(round($t1-$t0,1))") s ($("$PHOME/bin/postgres" --version))"
  (cd "$ROOT" && TRANSITOS_PG_PORT="$PORT" uv run --extra postgres python -m transitos.plumbing.pg_local ensure-db)
}

down() {
  if ! alive; then log "not running"; return 0; fi
  local p; p="$(head -1 "$DATA/postmaster.pid")"
  "$PHOME/bin/pg_ctl" -D "$DATA" -m fast -w -t 60 stop >&2 || { kill -9 "$p"; sleep 1; }
  if kill -0 "$p" 2>/dev/null; then log "pid $p still alive"; exit 1; fi
  log "stopped (pid $p)"
}

status() {
  if alive; then
    local p rss; p="$(head -1 "$DATA/postmaster.pid")"; rss="$(ps -o rss= -p "$p" | tr -d ' ')"
    echo "running pid=$p rss=$((rss / 1024))MB port$PORT=$(port_open && echo open || echo closed) version=$PG_VERSION"
  else
    echo "stopped"; return 3
  fi
}

reset() { down; rm -rf "$DATA" "$LOGFILE"; log "data dir removed"; }

purge() { (cd "$ROOT" && TRANSITOS_PG_PORT="$PORT" uv run --extra postgres python -m transitos.plumbing.pg_local purge); }

case "${1:-}" in
  fetch) fetch ;; up) up ;; down) down ;; status) status ;; reset) reset ;; purge) purge ;;
  *) echo "usage: $0 fetch|up|down|status|reset|purge" >&2; exit 2 ;;
esac
