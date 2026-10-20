#!/bin/bash
# shellcheck disable=SC2016  # wait_for conditions are single-quoted and eval-ed later, on purpose
# Runs INSIDE the image (tests/test-image.sh). tests/ is mounted at /t and scripts/ at /s.
set -uo pipefail
export LC_ALL=C

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}

for tool in steamcmd python3 curl pgrep pkill tar; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "flux-ase is on the PATH" "0" "$(status command -v flux-ase)"
check "the image's scripts are the repository's" "0" "$(status diff -r --exclude=__pycache__ /s /opt/flux)"
check "the unit tests pass with the image's Python" "0" "$(status bash -c 'cd /tmp && rm -rf repo && mkdir repo && cp -r /s repo/scripts && cp -r /t repo/tests && cd repo && ./tests/test-flux.sh')"

# A stand-in server: Source RCON on 127.0.0.1:27020 with the ini's admin password (ListPlayers,
# SaveWorld, Broadcast), a save file on SaveWorld and on SIGINT, then a hang like ARK's.
cat >/usr/local/bin/steamcmd <<'STUB'
#!/bin/bash
dir=""
while [ $# -gt 0 ]; do [ "$1" = "+force_install_dir" ] && dir="$2"; shift; done
[ -n "${dir}" ] || exit 0
mkdir -p "${dir}/ShooterGame/Binaries/Linux" "${dir}/steamapps"
printf '"AppState"\n{\n\t"buildid"\t\t"100"\n}\n' >"${dir}/steamapps/appmanifest_376030.acf"
cat >"${dir}/ShooterGame/Binaries/Linux/ShooterGameServer" <<'GAME'
#!/usr/bin/python3
import os, re, signal, socket, struct, sys, threading, time
gus = open("../../Saved/Config/LinuxServer/GameUserSettings.ini").read()
pw = re.search(r"^ServerAdminPassword=(.*)$", gus, re.M).group(1)
the_map = sys.argv[1].split("?")[0]
opts = dict(o.split("=", 1) for o in sys.argv[1].split("?")[1:] if "=" in o)
folder = "../../Saved/" + opts.get("AltSaveDirectoryName", "SavedArks")
def save():
    os.makedirs(folder, exist_ok=True)
    open(f"{folder}/{the_map}.ark", "a").write("saved\n")
def on_int(*_):
    save()
    time.sleep(120)   # ARK hangs after its save
signal.signal(signal.SIGINT, on_int)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 27020)); srv.listen(5)
def send(c, rid, kind, body):
    p = struct.pack("<ii", rid, kind) + body.encode() + b"\0\0"
    c.sendall(struct.pack("<i", len(p)) + p)
def client(c):
    authed = False
    while True:
        head = c.recv(4)
        if len(head) < 4: return
        data = c.recv(struct.unpack("<i", head)[0])
        rid, kind = struct.unpack("<ii", data[:8]); body = data[8:-2].decode()
        if kind == 3:
            authed = body == pw
            send(c, rid if authed else -1, 2, "")
        elif authed and kind == 2:
            if body == "SaveWorld": save()
            send(c, rid, 0, {"ListPlayers": "No Players Connected \n ", "SaveWorld": "World Saved \n "}.get(body.split(" ")[0], "Server received, But no response!! \n "))
while True:
    c, _ = srv.accept()
    threading.Thread(target=client, args=(c,), daemon=True).start()
GAME
chmod +x "${dir}/ShooterGame/Binaries/Linux/ShooterGameServer"
STUB
chmod +x /usr/local/bin/steamcmd

data=/mnt/ase/data
logf=/mnt/ase/server/flux-logs/flux.log
export FLUX_RESTART_BACKOFF=1 FLUX_UPDATE_CHECK_MINUTES=0 FLUX_STOP_GRACE=3 ASE_SESSION_NAME="Image Test"
/opt/flux/flux-entrypoint.sh >/tmp/entry.log 2>&1 &
entry=$!
check "the server comes up (its RCON answers)" "0" "$(status wait_for 'grep -q "\"running\"" ${data}/flux/state.json 2>/dev/null' 40)"
check "on the default map" "1" "$(pgrep -af 'ShooterGameServer ' | grep -c 'TheIsland?listen')"
check "without BattlEye by default" "1" "$(pgrep -af 'ShooterGameServer ' | grep -c -- '-NoBattlEye')"
check "Saved/ is the data volume" "${data}/Saved" "$(readlink /mnt/ase/server/ShooterGame/Saved)"
check "status over RCON" "True []" "$(flux-ase status | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["rcon"], d["players"])')"
check "an RCON command" "World Saved" "$(flux-ase rcon SaveWorld | python3 -c 'import json,sys;print(json.load(sys.stdin)["reply"])')"

flux-ase set-settings '{"map": "Ragnarok"}' >/dev/null
flux-ase restart >/dev/null
check "a panel restart saves, gives up on the hang, comes back on the new map" "0" "$(status wait_for 'pgrep -af "ShooterGameServer " | grep -q Ragnarok && grep -q "\"running\"" ${data}/flux/state.json' 60)"
check "not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/entry.log)"
check "a backup on the second start" "0" "$(status wait_for 'ls ${data}/flux/backups/*-ark.tar.gz' 5)"

# A restore while running: the server saves, a safety backup is taken, the archive replaces the
# saves, the server comes back.
echo before-restore >"${data}/Saved/SavedArks/Ragnarok/marker.txt"
backup="$(flux-ase backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][-1]["file"])')"
check "a restore is accepted while running" "True" "$(flux-ase restore "${backup}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["restarting"])')"
check "the server comes back after the restore" "0" "$(status wait_for '! test -e ${data}/flux/restore-pending && grep -q "\"running\"" ${data}/flux/state.json && grep -q "restored ${backup}" ${logf}' 60)"
check "a safety backup was taken first" "1" "$(find "${data}/flux/backups" -name '*-ark-pre-restore.tar.gz' | wc -l)"
check "the saves are the backup's" "1" "$(status test -e "${data}/Saved/SavedArks/Ragnarok/marker.txt")"

kill -TERM "${entry}"
check "a stop ends the supervisor" "0" "$(status wait_for '! kill -0 ${entry} 2>/dev/null' 30)"
check "with the world saved first, in the map's own folder" "0" "$(status grep -q saved "${data}/Saved/SavedArks/Ragnarok/Ragnarok.ark")"
check "and says it was saved" "Saved and stopped" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/flux/state.json"))["message"])')"
check "the log never shows the admin password" "0" "$(grep -c "ServerAdminPassword=$(flux-ase admin-password)" "${logf}")"

# --- flux.log is local; the banner ----------------------------------------------------------------
check "flux.log is on the install volume" "0" "$(status test -s "${logf}")"
check "and not on the synced data volume" "1" "$(status test -e "${data}/flux/flux.log")"
check "the banner is in the container output" "0" "$(status grep -q 'ARK: Survival Evolved server image by FluxCloud - https://runonflux.com' /tmp/entry.log)"
check "and in flux.log" "0" "$(status grep -q 'Host this and 30+ other games in one click: https://runonflux.com/games' "${logf}")"
check "at the container start and again for each generation" "1" "$([ "$(grep -c 'server image by FluxCloud' /tmp/entry.log)" -ge 2 ] && echo 1 || echo 0)"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ] || { echo '--- entrypoint log'; cat /tmp/entry.log; exit 1; }
