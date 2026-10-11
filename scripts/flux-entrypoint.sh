#!/bin/bash
# PID 1, and the supervisor of the ARK: Survival Evolved server.
#
#   - every start: SteamCMD update, ShooterGame/Saved linked to the data volume, the Workshop mods
#     downloaded and unpacked (fluxase.py sync-mods), a backup, GameUserSettings.ini and Game.ini
#     from the environment and the panel (fluxase.py apply), then the server
#   - SIGTERM/SIGINT from Docker: `SaveWorld` over RCON (0.6 s on a fresh world), then SIGINT. ARK
#     saves again on the signal, then hangs about a minute and dies with signal 11 (measured, every
#     time): the world is on disk long before Docker's 10 seconds, and a stop that does not end
#     within FLUX_STOP_GRACE is killed
#   - a new build on Steam or an updated mod: players warned in chat over RCON for
#     FLUX_WARN_MINUTES, then a restart onto it; ASE_RESTART_TIME = a daily one
#   - the server ending on its own: a new generation in place; FLUX_RESTART_MAX_ATTEMPTS
#     unplanned ones inside FLUX_RESTART_WINDOW end the container with 42
#   - a backup of the saves on every start and every FLUX_BACKUP_HOURS, after a SaveWorld
#
# THE RCON IS NOT PUBLISHED: the hub reaches it with an exec, on 127.0.0.1.

set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

FLUX_RESTART_MAX_ATTEMPTS="${FLUX_RESTART_MAX_ATTEMPTS:-5}"
FLUX_RESTART_WINDOW="${FLUX_RESTART_WINDOW:-3600}"
FLUX_RESTART_BACKOFF="${FLUX_RESTART_BACKOFF:-10}"
# ARK hangs ~60 s after it saved and before it dies: once the save is done there is nothing to
# wait for.
FLUX_STOP_GRACE="${FLUX_STOP_GRACE:-20}"
FLUX_BACKUP_HOURS="${FLUX_BACKUP_HOURS:-6}"
FLUX_BACKUP_KEEP="${FLUX_BACKUP_KEEP:-5}"
FLUX_UPDATE_CHECK_MINUTES="${FLUX_UPDATE_CHECK_MINUTES:-30}"
FLUX_WARN_MINUTES="${FLUX_WARN_MINUTES:-5}"

s="${FLUX_SERVER_DIR}"
d="${FLUX_DATA_DIR}"
ase() { python3 /opt/flux/fluxase.py "$@"; }
rcon() { python3 /opt/flux/fluxase.py rcon "$@" >/dev/null 2>&1; }

rm -f "${FLUX_RESTART_MARKER}"
mkdir -p "${s}" "${d}/Saved" "${FLUX_STATE_DIR}"

for var in ASE_PORT:7777 ASE_QUERY_PORT:27015; do
  name="${var%%:*}"
  if ! value="$(flux_valid_port "${!name:-}" "${var##*:}")"; then
    flux_log "ERROR ${name}='${!name:-}' is not a port; refusing to start"
    exit 42
  fi
  export "${name}=${value}"
done
if ! restart_at="$(flux_restart_time "${ASE_RESTART_TIME:-}")"; then
  flux_log "WARN ASE_RESTART_TIME='${ASE_RESTART_TIME}' is not HH:MM; no daily restart"
  restart_at=""
fi
flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}): game ${ASE_PORT}+$((ASE_PORT + 1))/udp, query ${ASE_QUERY_PORT}/udp, RCON on 127.0.0.1 only, daily restart ${restart_at:-off}"

game_pid=""
watch_pid=""
backup_pid=""
terminating=0
generation=0
restart_history=()

stop_server() {
  local pid
  pid="$(flux_game_pid)"
  pid="${pid:-${game_pid}}"
  [ -n "${pid}" ] || return 0
  rcon SaveWorld
  kill -INT "${pid}" 2>/dev/null
}

# shellcheck disable=SC2329  # invoked by the trap below
term_handler() {
  terminating=1
  flux_log "stop requested: saving the world and shutting down, no restart will follow"
  stop_server
  # Docker usually kills the container during ARK's post-save hang, before the loop below can
  # say "stopped": say it now, the world is on disk.
  ase state stopped "Saved and stopped"
}
trap term_handler TERM INT

update_server() {
  local attempt validate
  if flux_truthy "${SKIP_UPDATE:-}" && [ -x "${s}/${FLUX_GAME_BINARY}" ]; then
    flux_log "SKIP_UPDATE is set: not updating the server files"
    return 0
  fi
  ase state updating "Downloading the server from Steam (about 23 GB the first time)"
  flux_log "updating the server from Steam (app ${FLUX_STEAM_APP})"
  validate=""
  for attempt in 1 2 3 4 5; do
    # shellcheck disable=SC2086  # validate is "" or "validate"
    # app_info_update + a second app_update: SteamCMD answers "Missing configuration" to a first
    # app_update now and then (measured with this app and the ATS server).
    if steamcmd +force_install_dir "${s}" +login anonymous +app_info_update 1 +app_update "${FLUX_STEAM_APP}" +app_update "${FLUX_STEAM_APP}" ${validate} +quit; then
      flux_log "server build $(flux_installed_build "${s}/steamapps/appmanifest_${FLUX_STEAM_APP}.acf")"
      return 0
    fi
    [ "${terminating}" = "1" ] && return 1
    validate="validate"
    flux_log "WARN steamcmd failed (attempt ${attempt}/5); retrying in 5s with validate"
    sleep 5
  done
  if [ -x "${s}/${FLUX_GAME_BINARY}" ]; then
    flux_log "WARN could not update from Steam; starting the build already installed"
    return 0
  fi
  flux_log "ERROR could not install the server from Steam"
  return 1
}

link_saved() {
  local saved="${s}/ShooterGame/Saved"
  mkdir -p "${s}/ShooterGame"
  if [ -L "${saved}" ]; then
    [ "$(readlink "${saved}")" = "${d}/Saved" ] && return 0
    rm -f "${saved}"
  elif [ -d "${saved}" ]; then
    cp -an "${saved}/." "${d}/Saved/" 2>/dev/null
    rm -rf "${saved}"
  fi
  ln -s "${d}/Saved" "${saved}"
}

backup_saves() {
  local why="$1" out name
  [ "${FLUX_BACKUP_KEEP}" -gt 0 ] 2>/dev/null || return 0
  [ -d "${d}/Saved/SavedArks" ] || return 0
  mkdir -p "${FLUX_BACKUP_DIR}"
  name="$(date -u +%Y%m%d-%H%M%S)-ark.tar.gz"
  out="${FLUX_BACKUP_DIR}/${name}"
  # The world, players and tribes, and the config; not the game's own .bak copies or logs.
  if tar -C "${d}" --exclude='*.bak' --exclude='Saved/Logs' --exclude='*.tmp' -czf "${out}.part" Saved 2>/dev/null \
    && mv "${out}.part" "${out}"; then
    flux_log "backup (${why}): flux/backups/${name}, $(du -h "${out}" | cut -f1)"
  else
    rm -f "${out}.part"
    flux_log "WARN backup (${why}) failed"
  fi
  flux_prune "${FLUX_BACKUP_DIR}" "*-ark.tar.gz" "${FLUX_BACKUP_KEEP}"
}

# shellcheck disable=SC2329  # run in the background below
backupper() {
  [ "${FLUX_BACKUP_HOURS}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep $((FLUX_BACKUP_HOURS * 3600))
    rcon SaveWorld
    sleep 5
    backup_saves "every ${FLUX_BACKUP_HOURS}h"
  done
}

player_count() {
  python3 /opt/flux/fluxase.py rcon ListPlayers 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("players") or []))' 2>/dev/null
}

restart_with_warning() {
  local why="$1" left="${FLUX_WARN_MINUTES}"
  while [ "${left}" -gt 0 ] && [ "$(player_count)" != "0" ]; do
    rcon Broadcast "Server restart in ${left} minute$([ "${left}" = 1 ] || echo s): ${2}. Get somewhere safe."
    sleep 60
    left=$((left - 1))
  done
  printf '%s\n' "${why}" >"${FLUX_RESTART_MARKER}"
  flux_log "${why}: restarting the server"
  stop_server
}

# Mods updated on the Workshop since they were unpacked, by their Workshop time.
mods_outdated() {
  python3 - <<'PY' 2>/dev/null
import json, os, sys
sys.path.insert(0, "/opt/flux")
import fluxase as a
state = a.read_json(a.MODS_STATE, {}) or {}
ids = [m["id"] for m in state.get("mods", []) if m.get("installed")]
for i, d in a.mod_titles(ids).items():
    known = next((m for m in state["mods"] if m["id"] == i), {})
    if int(d.get("time_updated") or 0) > int(known.get("updated") or 0):
        print(i, d.get("title", ""))
PY
}

# shellcheck disable=SC2329  # run in the background below
watcher() {
  local now last_check next_restart="" build public i outdated
  for i in $(seq 1 900); do
    if rcon ListPlayers; then
      ase state running
      flux_log "the server is up (generation ${generation})"
      break
    fi
    sleep 2
    [ "${i}" = 900 ] && flux_log "WARN the server's RCON has not answered in 30 minutes"
  done
  last_check="$(date -u +%s)"
  [ -n "${restart_at}" ] && next_restart="$(flux_next_daily "$(date -u +%s)" "${restart_at}")"
  while true; do
    sleep 60
    now="$(date -u +%s)"
    if [ "${FLUX_UPDATE_CHECK_MINUTES}" -gt 0 ] 2>/dev/null && [ $((now - last_check)) -ge $((FLUX_UPDATE_CHECK_MINUTES * 60)) ]; then
      last_check="${now}"
      build="$(flux_installed_build "${s}/steamapps/appmanifest_${FLUX_STEAM_APP}.acf")"
      public="$(steamcmd +login anonymous +app_info_update 1 +app_info_print "${FLUX_STEAM_APP}" +quit 2>/dev/null | flux_public_build public)"
      if [ -n "${public}" ] && [ -n "${build}" ] && [ "${public}" != "${build}" ]; then
        restart_with_warning "planned: game update ${build} -> ${public}" "a game update"
        exit 0
      fi
      outdated="$(mods_outdated)"
      if [ -n "${outdated}" ]; then
        restart_with_warning "planned: mod update ($(head -1 <<<"${outdated}"))" "a mod update"
        exit 0
      fi
    fi
    if [ -n "${next_restart}" ] && [ "${now}" -ge "${next_restart}" ]; then
      restart_with_warning "planned: the daily restart (${restart_at})" "the daily restart"
      exit 0
    fi
  done
}

start_generation() {
  local line url="" args=()
  generation=$((generation + 1))
  rm -f "${FLUX_RESTART_MARKER}"

  update_server || return 1
  [ "${terminating}" = "1" ] && return 2
  link_saved
  ase state updating "Downloading and unpacking the mods"
  while IFS= read -r line; do
    flux_log "mods: ${line#\# }"
  done < <(ase sync-mods 2>&1)
  [ "${terminating}" = "1" ] && return 2
  backup_saves "start"
  while IFS= read -r line; do
    case "${line}" in
      "# "*) flux_log "config: ${line#\# }" ;;
      *\?listen\?*) url="${line}" ;;
    esac
  done < <(ase apply 2>&1)
  [ -n "${url}" ] || { flux_log "ERROR no launch line"; return 1; }

  args=(-server -log)
  if flux_truthy "${ASE_BATTLEYE:-false}"; then
    flux_log "BattlEye: on"
  else
    args+=(-NoBattlEye)
  fi
  if flux_truthy "${ASE_CROSSPLAY:-false}"; then
    # Epic players: the server has to tell Epic its public address.
    local ip
    ip="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null)"
    if [[ "${ip}" =~ ^[0-9.]+$ ]]; then
      args+=(-crossplay "-PublicIPForEpic=${ip}")
      flux_log "crossplay: on (Epic sees ${ip}); Epic players cannot use mods"
    else
      flux_log "WARN crossplay asked for, but the public address could not be found; off"
    fi
  fi
  [ -n "${ASE_CLUSTER_ID:-}" ] && [[ "${ASE_CLUSTER_ID}" =~ ^[A-Za-z0-9_-]{1,64}$ ]] && args+=("-clusterid=${ASE_CLUSTER_ID}" "-ClusterDirOverride=${d}/cluster")
  [ "${terminating}" = "1" ] && return 2

  ase state starting "Loading the map (about a minute)"
  flux_log "starting the server (generation ${generation}): ${url} ${args[*]}"
  (
    cd "${s}/ShooterGame/Binaries/Linux" || exit 1
    exec ./ShooterGameServer "${url}" "${args[@]}"
  ) &
  game_pid=$!
  watcher &
  watch_pid=$!
  backupper &
  backup_pid=$!
  return 0
}

sweep_generation() {
  local pid
  for pid in "${watch_pid}" "${backup_pid}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null
  done
  watch_pid="" backup_pid=""
  pkill -KILL -f 'ShooterGameServer ' 2>/dev/null
  game_pid=""
}

wait_for_generation() {
  local asked_at=0 now
  while kill -0 "${game_pid}" 2>/dev/null; do
    sleep 1 &
    wait $!
    if [ "${terminating}" = "1" ] || [ -f "${FLUX_RESTART_MARKER}" ]; then
      now="$(date -u +%s)"
      [ "${asked_at}" = "0" ] && asked_at="${now}"
      if [ $((now - asked_at)) -ge "${FLUX_STOP_GRACE}" ]; then
        flux_log "the server saved and was asked to stop ${FLUX_STOP_GRACE}s ago (ARK hangs after its save): ending it"
        kill -KILL "${game_pid}" 2>/dev/null
        break
      fi
    else
      asked_at=0
    fi
  done
  wait "${game_pid}" 2>/dev/null
}

while true; do
  if [ "${terminating}" = "1" ]; then
    ase state stopped
    exit 0
  fi
  start_generation
  started=$?
  if [ "${started}" = "2" ]; then
    ase state stopped
    exit 0
  elif [ "${started}" != "0" ]; then
    flux_log "ERROR the server could not be prepared; ending the container (42)"
    ase state crashed "The server could not be installed or started"
    exit 42
  fi
  wait_for_generation
  rc=$?
  reason=""
  [ -f "${FLUX_RESTART_MARKER}" ] && reason="$(cat "${FLUX_RESTART_MARKER}" 2>/dev/null)"
  sweep_generation
  rm -f "${FLUX_RESTART_MARKER}"
  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request (server exited ${rc})"
    ase state stopped
    exit 0
  fi
  [ -z "${reason}" ] && reason="the server ended on its own (exit ${rc})"
  if flux_restart_counts "${reason}"; then
    now="$(date -u +%s)"
    read -r -a restart_history <<<"$(flux_restarts_in_window "${now}" "${FLUX_RESTART_WINDOW}" "${restart_history[@]}")"
    restart_history+=("${now}")
    if [ "${#restart_history[@]}" -gt "${FLUX_RESTART_MAX_ATTEMPTS}" ]; then
      flux_log "${reason}: ${#restart_history[@]} restarts within ${FLUX_RESTART_WINDOW}s; ending the container (42)"
      ase state crashed "The server keeps stopping: ${reason}"
      exit 42
    fi
  fi
  ase state restarting "${reason}"
  flux_log "${reason}: restarting the server in place in ${FLUX_RESTART_BACKOFF}s"
  sleep "${FLUX_RESTART_BACKOFF}" &
  wait $!
done
