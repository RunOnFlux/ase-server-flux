#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh and fluxase.py. No Docker, no Steam, no game.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

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
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

check "a port" "7777" "$(flux_valid_port '' 7777)"
check "a daily restart" "05:00" "$(flux_restart_time 5:00)"
check "a mod update does not count as a crash" "1" "$(status flux_restart_counts 'planned: mod update (1 x)')"

data="${tmp}/data"
ase() {
  local vars=()
  while [[ "${1:-}" == *=* ]]; do vars+=("$1"); shift; done
  env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" "${vars[@]}" python3 scripts/fluxase.py "$@"
}
gus="${data}/Saved/Config/LinuxServer/GameUserSettings.ini"
game="${data}/Saved/Config/LinuxServer/Game.ini"
iniv() { python3 -c 'import sys; sys.path.insert(0,"scripts"); import fluxase as a; print(a.ini_get(open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]))' "$@"; }

url="$(ase ASE_SESSION_NAME="My Ark" ASE_MAX_PLAYERS=20 ASE_XP=2 ASE_PVE=true ASE_MAP=Ragnarok apply | tail -1)"
check "the launch URL" "Ragnarok?listen?SessionName=My Ark?Port=7777?QueryPort=27015?MaxPlayers=20?RCONEnabled=True?RCONPort=27020?ServerAdminPassword=$(ase admin-password)" "${url}"
check "the session name in the ini" "My Ark" "$(iniv "${gus}" SessionSettings SessionName)"
check "max players in its own section" "20" "$(iniv "${gus}" /Script/Engine.GameSession MaxPlayers)"
check "XP" "2.000000" "$(iniv "${gus}" ServerSettings XPMultiplier)"
check "PvE" "True" "$(iniv "${gus}" ServerSettings serverPVE)"
check "RCON is on" "True 27020" "$(iniv "${gus}" ServerSettings RCONEnabled) $(iniv "${gus}" ServerSettings RCONPort)"
check "the admin password is made and kept" "$(ase admin-password)" "$(iniv "${gus}" ServerSettings ServerAdminPassword)"
check "an unknown map is TheIsland" "TheIsland" "$(ase ASE_MAP=Narnia apply | tail -1 | cut -d'?' -f1)"

# The server's own lines are kept, ours replaced once, case-insensitively.
printf '[ServerSettings]\nxpmultiplier=9\nSomethingElse=1\n[Other]\nX=1\n' >"${gus}"
ase ASE_XP=3 apply >/dev/null
check "one XP line, ours" "1 3.000000" "$(grep -ci '^xpmultiplier=' "${gus}") $(iniv "${gus}" ServerSettings XPMultiplier)"
check "their key kept" "1" "$(iniv "${gus}" ServerSettings SomethingElse)"
check "their section kept" "1" "$(iniv "${gus}" Other X)"

check "the panel takes settings" "0" "$(status ase set-settings '{"taming": 5, "babyMature": 10, "map": "Fjordur", "password": "pw"}')"
ase apply >/dev/null
check "taming" "5.000000" "$(iniv "${gus}" ServerSettings TamingSpeedMultiplier)"
check "breeding goes to Game.ini" "10.000000" "$(iniv "${game}" /script/shootergame.shootergamemode BabyMatureSpeedMultiplier)"
check "the panel's map" "Fjordur" "$(ase apply | tail -1 | cut -d'?' -f1)"
check "the join password" "pw" "$(iniv "${gus}" ServerSettings ServerPassword)"
check "a password with ? is refused (it would break the URL)" "2" "$(status ase set-settings '{"password": "a?b"}')"
check "out of range" "2" "$(status ase set-settings '{"xp": -1}')"
check "an unknown map" "2" "$(status ase set-settings '{"map": "Narnia"}')"
check "settings hide the password" "(set)" "$(ase settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["values"]["password"])')"

# Mods.
check "mod ids from links" '["731604991", "1404697612"]' "$(ase set-mods '{"mods": ["https://steamcommunity.com/sharedfiles/filedetails/?id=731604991", "1404697612", "731604991"]}' | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["mods"]))')"
ase ASE_MODS="111,731604991" apply >/dev/null
check "ActiveMods: env first, then the panel's, once" "111,731604991,1404697612" "$(iniv "${gus}" ServerSettings ActiveMods)"

# A .z file, written the way the game's tools write one, and a mod.info.
python3 - "${tmp}" <<'PY'
import os, struct, sys, zlib
d = sys.argv[1]
raw = b"hello ark " * 10000
chunks = [raw[i:i + 32768] for i in range(0, len(raw), 32768)]
comp = [zlib.compress(c) for c in chunks]
head = b"\xC1\x83\x2A\x9E\x00\x00\x00\x00" + struct.pack("<qqq", 32768, sum(map(len, comp)), len(raw))
head += b"".join(struct.pack("<qq", len(c), len(u)) for c, u in zip(comp, chunks))
src = os.path.join(d, "ws", "steamapps", "workshop", "content", "346110", "999", "WindowsNoEditor")
os.makedirs(os.path.join(src, "Sub"))
open(os.path.join(src, "Sub", "file.uasset.z"), "wb").write(head + b"".join(comp))
open(os.path.join(src, "Sub", "file.uasset.z.uncompressed_size"), "w").write(str(len(raw)))
name = b"MyMod\x00"
maps = [b"MyMap\x00"]
info = struct.pack("<I", len(name)) + name + struct.pack("<I", len(maps)) + b"".join(struct.pack("<I", len(m)) + m for m in maps)
open(os.path.join(src, "mod.info"), "wb").write(info)
open(os.path.join(src, "plain.txt"), "w").write("x")
PY
cat >"${tmp}/inst.py" <<'PY'
import os, sys
sys.path.insert(0, "scripts")
import fluxase as a
a.WORKSHOP_CONTENT = os.path.join(sys.argv[1], "ws", "steamapps", "workshop", "content", "346110")
a.MODS_DIR = os.path.join(sys.argv[1], "Mods")
print(a.install_mod("999", "My Mod"), a.install_mod("999", "My Mod"))
PY
check "a mod installs once, then is up to date" "True False" "$(python3 "${tmp}/inst.py" "${tmp}")"
check "its .z is unpacked" "100000" "$(wc -c <"${tmp}/Mods/999/Sub/file.uasset")"
check "the size files are not copied" "1" "$(status test -e "${tmp}/Mods/999/Sub/file.uasset.z.uncompressed_size")"
check "plain files are copied" "x" "$(cat "${tmp}/Mods/999/plain.txt")"
check "the .mod file names the mod and its path" "0" "$(status bash -c "grep -aq 'My Mod' '${tmp}/Mods/999.mod' && grep -aq '../../../ShooterGame/Content/Mods/999' '${tmp}/Mods/999.mod'")"
check "and carries its map" "0" "$(status grep -aq 'MyMap' "${tmp}/Mods/999.mod")"
check "the .mod starts with the id" "999" "$(python3 -c 'import struct,sys;print(struct.unpack("<Q", open(sys.argv[1],"rb").read(8))[0])' "${tmp}/Mods/999.mod")"

# RCON replies.
py() { PYTHONPATH=scripts python3 -c "import fluxase as t; $1"; }
check "no players" "[]" "$(py 'print(t.parse_players("No Players Connected \n "))')"
check "players" "Rex 76561198000000001,Bob, Jr 76561198000000002" "$(py 'print(",".join(p["name"]+" "+p["id"] for p in t.parse_players("\n0. Rex, 76561198000000001\n1. Bob, Jr, 76561198000000002\n ")))')"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
