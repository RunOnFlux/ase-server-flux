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
check "the launch URL" "Ragnarok?listen?SessionName=My Ark?Port=7777?QueryPort=27015?MaxPlayers=20?RCONEnabled=True?RCONPort=27020?ServerAdminPassword=$(ase admin-password)?AltSaveDirectoryName=SavedArks/Ragnarok" "${url}"
check "the URL's passwords can be masked for the log" "Ragnarok?listen?ServerAdminPassword=***?AltSaveDirectoryName=x?ServerPassword=***" "$(ase mask 'Ragnarok?listen?ServerAdminPassword=abc?AltSaveDirectoryName=x?ServerPassword=pw')"
check "an EMPTY env value does not pin a setting" "panel" "$(ase set-settings '{"pve": true}' >/dev/null; ase ASE_PVE= settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["sources"]["pve"])')"
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
check "the panel's map wins over ASE_MAP (the first map)" "Fjordur panel" "$(ase ASE_MAP=Ragnarok apply | tail -1 | cut -d'?' -f1) $(ase ASE_MAP=Ragnarok settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["mapSource"])')"
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
print(a.install_mod("999", "My Mod", version=1700000000), a.install_mod("999", "My Mod", version=1700000000), os.path.isdir(os.path.join(a.WORKSHOP_CONTENT, "999")))
PY
check "a mod installs once, then is up to date, and its download is removed" "True False False" "$(python3 "${tmp}/inst.py" "${tmp}")"
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

# Saves: one folder per map, an old flat layout moved once.
mkdir -p "${data}/Saved/SavedArks" "${data}/flux/backups"
sa="${data}/Saved/SavedArks"
echo world >"${sa}/Fjordur.ark"; echo w2 >"${sa}/Ragnarok.ark"; echo bak >"${sa}/Fjordur_AntiCorruptionBackup.bak"
echo x >"${sa}/1.arkprofile"; echo x >"${sa}/2.arkprofile"; echo x >"${sa}/9.arktribe"
for i in 1 2 3 4 5 6; do echo d >"${sa}/Fjordur_0${i}.10.2026_10.00.00.ark"; touch -d "2026-10-0${i}" "${sa}/Fjordur_0${i}.10.2026_10.00.00.ark"; done
ase set-settings '{"map": "Fjordur"}' >/dev/null
ase apply >"${tmp}/apply"
check "the flat layout is moved into one folder per map" "world w2" "$(cat "${sa}/Fjordur/Fjordur.ark") $(cat "${sa}/Ragnarok/Ragnarok.ark")"
check "players and tribes are copied beside every world" "2 1 2" "$(find "${sa}/Fjordur" -name '*.arkprofile' | wc -l) $(find "${sa}/Fjordur" -name '*.arktribe' | wc -l) $(find "${sa}/Ragnarok" -name '*.arkprofile' | wc -l)"
check "the shared originals are kept aside" "0" "$(status test -f "${sa}/.flux-premigration/1.arkprofile")"
check "ARK's own copies move with their world" "0" "$(status test -f "${sa}/Fjordur/Fjordur_AntiCorruptionBackup.bak")"
check "the newest 4 dated copies are kept" "4" "$(find "${sa}/Fjordur" -name 'Fjordur_??.??.2026_*.ark' | wc -l)"
check "and the oldest go" "1" "$(status test -e "${sa}/Fjordur/Fjordur_01.10.2026_10.00.00.ark")"
check "the migration runs once" "0" "$(grep -c 'one folder per map' <(ase apply))"
check "the URL saves into the map's folder" "1" "$(ase apply | grep -cE 'AltSaveDirectoryName=SavedArks/Fjordur(\?|$)')"
check "a backup is due with none yet" "0" "$(status ase backup-due 60)"
echo x >"${data}/flux/backups/20261011-000000-ark.tar.gz"
check "saves: per map, with its players and dated copies, and backups" "Fjordur True 2 1 4 1" "$(ase saves | python3 -c 'import json,sys;d=json.load(sys.stdin);m=[x for x in d["maps"] if x["map"]=="Fjordur"][0];print(m["map"], m["active"], m["profiles"], m["tribes"], m["dated"], len(d["backups"]))')"
check "saves report disk use" "True" "$(ase saves | python3 -c 'import json,sys;d=json.load(sys.stdin)["disk"];print(d["saves"] > 0)')"

# A Workshop map, and transfers between this server's maps.
check "a Workshop map by link" "0" "$(status ase set-settings '{"modMap": "https://steamcommunity.com/sharedfiles/filedetails/?id=1234567"}')"
ase apply >"${tmp}/apply"
check "it is loaded with -MapModID and saved in its own folder" "ARG -MapModID=1234567 1" "$(grep '^ARG -MapModID' "${tmp}/apply") $(grep -c 'AltSaveDirectoryName=SavedArks/Mod_1234567' "${tmp}/apply")"
check "and is in ActiveMods" "0" "$(status grep -q 'ActiveMods=.*1234567' "${gus}")"
check "an official map clears it" "0" "$(ase set-settings '{"map": "Ragnarok"}' >/dev/null; ase apply | grep -c 'MapModID')"
ase set-settings '{"mapTransfers": true}' >/dev/null
check "map transfers add a one-server cluster" "2" "$(ase apply | grep -c '^ARG -clust\|^ARG -ClusterDir')"

# Backups: the rotation never loses the last good day or week.
check "a crash loop's backups keep the day-old and week-old ones" "b1,b2,b5,b9" "$(py 'n=["b1","b2","b3","b4","b5","b6","b7","b8","b9"]; tm={"b1":1000,"b2":990,"b3":980,"b4":970,"b5":-90000,"b6":-90010,"b7":-90020,"b8":-90030,"b9":-700000}; print(",".join(x for x in n if x not in t.backups_to_prune(n,tm,2,now=1000)))')"
touch "${data}/flux/backups/20261011-000000-ark.tar.gz"
check "a start backup is skipped right after another" "1" "$(status ase backup-due 60)"

# Restore: checked by the command, applied by the supervisor's start.
mkdir -p "${tmp}/bk/Saved/SavedArks/Ragnarok" && echo restored >"${tmp}/bk/Saved/SavedArks/Ragnarok/Ragnarok.ark"
tar -C "${tmp}/bk" -czf "${data}/flux/backups/20261012-000000-ark.tar.gz" Saved
check "a restore of a missing backup is refused" "2" "$(status ase restore 20990101-000000-ark.tar.gz)"
check "a path is refused" "2" "$(status ase restore ../x.tar.gz)"
check "a restore is accepted while stopped" "start" "$(ase restore 20261012-000000-ark.tar.gz | python3 -c 'import json,sys;print(json.load(sys.stdin)["appliesOn"])')"
check "and is pending" "True" "$(ase status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
ase backups-prune 0 >/dev/null
check "the backup a restore waits for is never pruned" "0" "$(status test -f "${data}/flux/backups/20261012-000000-ark.tar.gz")"
check "a failed safety backup restores nothing" "False" "$(ase restore-apply failed >/dev/null; ase status | python3 -c 'import json,sys;print(json.load(sys.stdin)["lastRestore"]["ok"])')"
check "and the current saves stay" "1" "$(status grep -qs restored "${sa}/Ragnarok/Ragnarok.ark")"
ase restore 20261012-000000-ark.tar.gz >/dev/null
ase restore-apply ok >/dev/null
check "the start applies it" "restored" "$(cat "${sa}/Ragnarok/Ragnarok.ark")"
check "and says so" "True 20261012-000000-ark.tar.gz" "$(ase status | python3 -c 'import json,sys;r=json.load(sys.stdin)["lastRestore"];print(r["ok"], r["file"])')"
check "once" "1" "$(status test -e "${data}/flux/restore-pending")"
check "nothing waits for a restart" "1" "$(status ase pending)"

# Updates that failed are not retried for 6 h.
check "an update is due" "0" "$(status ase update-due 100 200)"
ase update-done >/dev/null
check "a target that did not install is remembered" "200" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["target"])' "${data}/flux/update-failed.json")"
check "and not due again" "1" "$(status ase update-due 100 200)"
check "a newer build is" "0" "$(status ase update-due 100 300)"

# RCON replies.
check "Epic players are listed too" "Rex steam,Eve epic" "$(py 'print(",".join(p["name"]+" "+p["platform"] for p in t.parse_players("0. Rex, 76561198000000001\n1. Eve, 0123456789abcdef0123456789abcdef\n")))')"
check "one command per call: | is refused" "2" "$(status ase rcon 'Broadcast hi | DoExit')"
# --- flux.log: local, rotated, migrated; the start-up banner -------------------------------------
lt="$(mktemp -d)"
rule='====================================================================='
want="$(printf '%s\n %s server image by FluxCloud - https://runonflux.com\n Host this and 30+ other games in one click: https://runonflux.com/games\n%s' "${rule}" "Test Game" "${rule}")"
banner_out="$(FLUX_LOG="${lt}/logs/flux.log" flux_banner "Test Game")"
check "the banner is exactly the FluxCloud text" "${want}" "${banner_out}"
check "the banner is ASCII only" "0" "$(printf '%s' "${banner_out}" | LC_ALL=C grep -c -P '[^\x00-\x7F]')"
check "the banner goes to flux.log too" "${want}" "$(cat "${lt}/logs/flux.log")"
# shellcheck source=scripts/flux-lib.sh
check "flux.log lives in flux-logs on the install volume by default" "/x/flux-logs/flux.log" "$(unset FLUX_LOG FLUX_LOG_DIR; FLUX_SERVER_DIR=/x; source scripts/flux-lib.sh; echo "${FLUX_LOG}")"
check "an empty FLUX_LOG writes no file" "" "$(FLUX_LOG="" flux_log hello >/dev/null; ls "${lt}/none" 2>/dev/null)"
for i in $(seq 1 40); do FLUX_LOG="${lt}/logs/flux.log" FLUX_LOG_MAX_BYTES=500 flux_log "line number ${i} padding padding padding" >/dev/null; done
check "flux.log rotates to one .1" "1" "$([ -s "${lt}/logs/flux.log.1" ] && echo 1 || echo 0)"
check "and the live file stays under the cap" "1" "$([ "$(stat -c %s "${lt}/logs/flux.log")" -le 600 ] && echo 1 || echo 0)"
check "no second rotated copy" "1" "$(status test -e "${lt}/logs/flux.log.2")"
printf 'old 1\nold 2\n' >"${lt}/old.log"
printf 'new 1\n' >"${lt}/mig.log"
FLUX_LOG="${lt}/mig.log" flux_log_migrate "${lt}/old.log" "${lt}/absent.log"
check "an old flux.log is appended to the new one" "new 1 old 1 old 2" "$(tr '\n' ' ' <"${lt}/mig.log" | sed 's/ $//')"
check "and deleted from the data volume" "1" "$(status test -e "${lt}/old.log")"
check "migrating with nothing to migrate is harmless" "0" "$(FLUX_LOG="${lt}/mig.log" status flux_log_migrate "${lt}/old.log")"
rm -rf "${lt}"

# ---- Launch options (1.1.0): the panel's flags reach the launch line as ARG lines, never the URL.
rm -f "${data}/flux/settings.json"
check "no flags by default" "" "$(ase apply | grep '^ARG ' | tr '\n' ' ')"
check "a known flag is accepted" "0" "$(status ase set-settings '{"launch": {"forceAllowCaveFlyers": true, "culture": "pt_BR", "maxSaveBackups": 12}}')"
check "it becomes an ARG line, in table order" "ARG -ForceAllowCaveFlyers ARG -culture=pt_BR ARG -MaxNumOfSaveBackups=12" "$(ase apply | grep '^ARG ' | tr '\n' ' ' | sed 's/ $//')"
check "false removes the flag" "" "$(ase set-settings '{"launch": {"forceAllowCaveFlyers": false, "culture": null, "maxSaveBackups": null}}' >/dev/null; ase apply | grep '^ARG ' | tr '\n' ' ')"
check "an unknown launch option is refused" "2" "$(status ase set-settings '{"launch": {"nope": true}}')"
check "a bad word value is refused" "2" "$(status ase set-settings '{"launch": {"culture": "a b"}}')"
check "free flags are kept and listed" "ARG -UseVivox ARG -foo=1" "$(ase set-settings '{"extraArgs": "-UseVivox -foo=1 -usevivox"}' >/dev/null; ase apply | grep '^ARG ' | tr '\n' ' ' | sed 's/ $//')"
check "the owned flags are refused, with the flag named" "2" "$(status ase set-settings '{"extraArgs": "-Port=1234"}')"
check "-NoBattlEye is owned" "2" "$(status ase set-settings '{"extraArgs": "-NoBattlEye"}')"
check "a ? option is refused" "2" "$(status ase set-settings '{"extraArgs": "?Foo=1"}')"
# shellcheck disable=SC2016
check "shell characters are refused" "2" "$(status ase set-settings '{"extraArgs": "-a=$(id)"}')"
check "a flag set twice keeps the table's" "ARG -ForceAllowCaveFlyers" "$(ase set-settings '{"launch": {"forceAllowCaveFlyers": true}, "extraArgs": "-forceallowcaveflyers"}' >/dev/null; ase apply | grep '^ARG ' | tr '\n' ' ' | sed 's/ $//')"
check "the panel reads its launch state back" "True" "$(ase settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["launch"]["forceAllowCaveFlyers"])')"
rm -f "${data}/flux/settings.json"

# ---- The catalog: every entry is valid, unique, and reaches the right file and section.
check "the catalog is well formed" "ok" "$(python3 - <<'PY'
import json, sys
sys.path.insert(0, "scripts")
import fluxase as a
c = json.load(open("scripts/catalog.json"))
groups = {g["id"] for g in c["groups"]}
seen = set()
for s in c["settings"]:
    assert s["kind"] in ("bool", "int", "float"), s
    assert s["group"] in groups, s
    assert (s["file"], s["ini"].lower()) not in seen, s
    seen.add((s["file"], s["ini"].lower()))
    assert s["key"] in a.SETTINGS, s
    if s["kind"] != "bool":
        assert s["min"] <= s["official"] <= s["max"], s
print("ok")
PY
)"
check "a Game.ini catalog setting lands in Game.ini" "0.500000" "$(ase set-settings '{"killXp": 0.5}' >/dev/null; ase apply >/dev/null; iniv "${game}" /script/shootergame.shootergamemode KillXPMultiplier)"
check "a ServerSettings catalog setting lands in the gus" "True" "$(ase set-settings '{"pickup": true}' >/dev/null; ase apply >/dev/null; iniv "${gus}" ServerSettings AlwaysAllowStructurePickup)"
check "the MOTD duration goes to its own section" "30" "$(ase set-settings '{"motdDuration": 30}' >/dev/null; ase apply >/dev/null; iniv "${gus}" MessageOfTheDay Duration)"
rm -f "${data}/flux/settings.json"

# Section names are case-insensitive, like Unreal's: a Game.ini written with the capitalised section is updated, not duplicated.
printf '[/Script/ShooterGame.ShooterGameMode]\nKillXPMultiplier=9\nOther=1\n' >"${game}"
ase set-settings '{"killXp": 0.5}' >/dev/null; ase apply >/dev/null
check "a capitalised Game.ini section is updated in place" "1 0.500000 1" "$(grep -ci 'shootergamemode\]' "${game}") $(iniv "${game}" /script/shootergame.shootergamemode KillXPMultiplier) $(iniv "${game}" /Script/ShooterGame.ShooterGameMode Other)"
check "and the stale line is gone" "1" "$(grep -ci '^KillXPMultiplier=' "${game}")"
check "a whole number setting refuses a fraction" "2" "$(status ase set-settings '{"maxTamedDinos": 5000.5}')"
rm -f "${data}/flux/settings.json"

# Back to the game's default: null deletes the line from the ini (once), setting it again writes it.
printf '[/script/shootergame.shootergamemode]\nKillXPMultiplier=9\nOther=1\n' >"${game}"
ase set-settings '{"killXp": 2}' >/dev/null; ase apply >/dev/null
ase set-settings '{"killXp": null}' >/dev/null; ase apply >/dev/null
check "null removes the setting's line, keeps the others" "0 1" "$(grep -ci '^KillXPMultiplier=' "${game}") $(iniv "${game}" /script/shootergame.shootergamemode Other)"
check "the reset is spent after one apply" "0" "$(grep -c '"reset"' "${data}/flux/settings.json")"
rm -f "${data}/flux/settings.json"

# ---- Phase 2: indexed keys and repeated-key lists.
rm -f "${data}/flux/settings.json"
printf '[/script/shootergame.shootergamemode]\nConfigOverrideItemMaxQuantity=(ItemClassString="Hand_C",Quantity=(MaxItemQuantity=1,bIgnoreMultiplier=False))\nPreventTransferForClassNames="Mine_C"\nOther=1\n' >"${game}"
check "the panel takes a list" "0" "$(status ase set-settings '{"lists": {"itemMaxQuantity": [{"className": "PrimalItemAmmo_ArrowTranq_C", "maxQuantity": 543, "ignoreMultiplier": true}, {"className": "PrimalItemResource_Wood_C", "maxQuantity": 1000}]}}')"
ase apply >/dev/null
check "one rendered line per entry, the old lines of that key replaced" "2 1 0" "$(grep -c '^ConfigOverrideItemMaxQuantity=' "${game}") $(grep -c 'MaxItemQuantity=543,bIgnoreMultiplier=True' "${game}") $(grep -c 'MaxItemQuantity=1,' "${game}")"
check "a list key the panel never managed keeps the owner's own line" "1" "$(grep -c '^PreventTransferForClassNames="Mine_C"' "${game}")"
check "and other keys stay" "1" "$(iniv "${game}" /script/shootergame.shootergamemode Other)"
check "an emptied list deletes its lines, once" "0" "$(ase set-settings '{"lists": {"itemMaxQuantity": []}}' >/dev/null; ase apply >/dev/null; grep -c '^ConfigOverrideItemMaxQuantity=' "${game}")"
check "and is not managed afterwards" "0" "$(grep -c 'itemMaxQuantity' "${data}/flux/settings.json" 2>/dev/null)"
check "a class name with quotes is refused" "2" "$(status ase set-settings '{"lists": {"dinoDamage": [{"className": "Rex\"_C", "multiplier": 2}]}}')"
check "a class name with a space is refused" "2" "$(status ase set-settings '{"lists": {"dinoDamage": [{"className": "Rex C", "multiplier": 2}]}}')"
check "a missing field is refused" "2" "$(status ase set-settings '{"lists": {"dinoDamage": [{"className": "Rex_C"}]}}')"
check "a class listed twice is refused" "2" "$(status ase set-settings '{"lists": {"dinoDamage": [{"className": "Rex_C", "multiplier": 2}, {"className": "rex_c", "multiplier": 3}]}}')"
check "an engram override with nothing to change is refused" "2" "$(status ase set-settings '{"lists": {"engramOverride": [{"className": "EngramEntry_Campfire_C"}]}}')"
check "an unknown list is refused" "2" "$(status ase set-settings '{"lists": {"nope": []}}')"
ase set-settings '{"lists": {"engramOverride": [{"className": "EngramEntry_StoneHatchet_C", "hidden": false, "pointsCost": 3, "levelRequirement": 3, "removePrereq": true}], "spawnWeight": [{"className": "Bronto", "weight": 10, "limitPercent": 0.5}], "npcReplacement": [{"fromClass": "A_C", "toClass": "B_C"}], "preventBreeding": [{"className": "Rex_C"}]}}' >/dev/null
ase apply >/dev/null
check "engram override line" '1' "$(grep -c '^OverrideNamedEngramEntries=(EngramClassName="EngramEntry_StoneHatchet_C",EngramHidden=False,EngramPointsCost=3,EngramLevelRequirement=3,RemoveEngramPreReq=True)$' "${game}")"
check "spawn weight line" '1' "$(grep -c '^DinoSpawnWeightMultipliers=(DinoNameTag=Bronto,SpawnWeightMultiplier=10.0,OverrideSpawnLimitPercentage=True,SpawnLimitPercentage=0.5)$' "${game}")"
check "replacement and prevent lines" '1 1' "$(grep -c '^NPCReplacements=(FromClassName="A_C",ToClassName="B_C")$' "${game}") $(grep -c '^PreventBreedingForClassNames="Rex_C"$' "${game}")"
check "the panel reports the list schemas and what is already in the file" "ok" "$(ase settings | python3 -c 'import json,sys;d=json.load(sys.stdin);print("ok" if "dinoDamage" in d["listSchemas"] and d["listLinesInFile"]["preventTransfer"]==1 and d["lists"]["preventBreeding"] else "no")')"
rm -f "${data}/flux/settings.json"
# Indexed keys are plain keys with [n] in the name.
check "an indexed stat setting is written and read back" "2.500000 2.500000" "$(ase set-settings '{"statPlayer7": 2.5}' >/dev/null; ase apply >/dev/null; iniv "${game}" /script/shootergame.shootergamemode 'PerLevelStatsMultiplier_Player[7]') $(ase settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["file"]["statPlayer7"])')"
check "a stat multiplier of 0 is refused (the game would read it as 1)" "2" "$(status ase set-settings '{"statPlayer7": 0}')"
rm -f "${data}/flux/settings.json"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
