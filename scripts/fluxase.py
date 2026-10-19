#!/usr/bin/env python3
"""Every decision about the ARK: Survival Evolved server's files, its mods, its RCON, and the hub's
commands.

    fluxase.py apply                   GameUserSettings.ini and Game.ini from the environment and the
                                       panel; print the launch line's map URL (before every start)
    fluxase.py sync-mods               download, unpack and register the Workshop mods (before every start)
    fluxase.py state <state> [msg]     record what the supervisor is doing (flux/state.json)
    fluxase.py admin-password          the admin password (made once and kept, unless ASE_ADMIN_PASSWORD)

    flux-ase status                    state, players, map, version (one JSON document)
    flux-ase rcon <command...>         one RCON command, its reply as JSON
    flux-ase settings                  every setting the panel edits, with where its value comes from
    flux-ase set-settings '<json>'     save settings (applied on the next start)
    flux-ase mods / set-mods '<json>'  the Workshop mods: {"mods": ["731604991", ...]}
    flux-ase saves                     every map's world, players and tribes, ARK's own copies, disk use
    flux-ase backups                   our backups (flux/backups), newest first
    flux-ase restore <backup>          restore one: a safety backup first, then the save is replaced
                                       and the server started again (applied by the supervisor)
    flux-ase restart                   save, stop and start again
    flux-ase pending                   exit 0 when settings, mods or a restore wait for a restart

THE RCON IS NOT PUBLISHED: Source RCON on 127.0.0.1, reached by the hub's exec. The admin password
(also the in-game `enablecheats` password) is the RCON password.

Precedence: an environment variable that is SET (and not empty) is applied on every start; one
that is not set or empty leaves the panel's value, then the file's own. The marketplace writes
declared parameters even when left blank, so an empty value never pins a setting.

EVERY MAP HAS ITS OWN SAVE FOLDER (Saved/SavedArks/<Map>/, ARK's ?AltSaveDirectoryName): its world,
its players and its tribes. Without it the .arkprofile/.arktribe files are shared by every map and
a character's levels and engrams travel between maps. A server from image 1.0.0/1.0.1 is moved on
its first start (each world into its folder, the shared players and tribes copied beside it).
"""

import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

SERVER_DIR = os.environ.get("FLUX_SERVER_DIR", "/mnt/ase/server")
DATA_DIR = os.environ.get("FLUX_DATA_DIR", "/mnt/ase/data")
STATE_DIR = os.environ.get("FLUX_STATE_DIR", os.path.join(DATA_DIR, "flux"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
MODS_FILE = os.path.join(STATE_DIR, "mods.json")
MODS_STATE = os.path.join(STATE_DIR, "mods-state.json")
ADMIN_SECRET = os.path.join(STATE_DIR, "admin.secret")
SAVED = os.path.join(DATA_DIR, "Saved")
CONFIG_DIR = os.path.join(SAVED, "Config", "LinuxServer")
GUS = os.path.join(CONFIG_DIR, "GameUserSettings.ini")
GAME_INI = os.path.join(CONFIG_DIR, "Game.ini")
MODS_DIR = os.path.join(SERVER_DIR, "ShooterGame", "Content", "Mods")
WORKSHOP = os.path.join(SERVER_DIR, "workshop")
WORKSHOP_CONTENT = os.path.join(WORKSHOP, "steamapps", "workshop", "content", "346110")
RCON_PORT = int(os.environ.get("ASE_RCON_PORT", "27020"))
SAVED_ARKS = os.path.join(SAVED, "SavedArks")
BACKUP_DIR = os.environ.get("FLUX_BACKUP_DIR", os.path.join(STATE_DIR, "backups"))
RESTORE_PENDING = os.path.join(STATE_DIR, "restore-pending")
RESTORE_RESULT = os.path.join(STATE_DIR, "restore-result.json")
UPDATE_TARGET = os.path.join(STATE_DIR, "update-target.json")
UPDATE_FAILED = os.path.join(STATE_DIR, "update-failed.json")
WORKSHOP_ACF = os.path.join(WORKSHOP, "steamapps", "workshop", "appworkshop_346110.acf")
# ARK's own dated copies of a world, which it never deletes: "<Map>_DD.MM.YYYY_HH.MM.SS.ark".
DATED_RE = re.compile(r"^(?P<map>.+)_\d{2}\.\d{2}\.\d{4}_\d{2}\.\d{2}\.\d{2}\.ark$")
DATED_KEEP = int(os.environ.get("FLUX_DATED_KEEP", "4"))
FAILED_RETRY_SECONDS = 6 * 3600

# The maps a server can run without owning anything (players need the DLC for the starred ones).
MAPS = {
    "TheIsland": ("The Island", False), "TheCenter": ("The Center", False), "Ragnarok": ("Ragnarok", False),
    "Valguero_P": ("Valguero", False), "CrystalIsles": ("Crystal Isles", False), "LostIsland": ("Lost Island", False),
    "Fjordur": ("Fjordur", False), "ScorchedEarth_P": ("Scorched Earth", True), "Aberration_P": ("Aberration", True),
    "Extinction": ("Extinction", True), "Genesis": ("Genesis: Part 1", True), "Gen2": ("Genesis: Part 2", True),
}

SS = "ServerSettings"
SESSION = "/Script/Engine.GameSession"
MODE = "/script/shootergame.shootergamemode"
# setting: (file, section, key, kind, limit)
SETTINGS = {
    "sessionName": ("gus", "SessionSettings", "SessionName", "text", 64),
    "maxPlayers": ("gus", SESSION, "MaxPlayers", "int", 255),
    "password": ("gus", SS, "ServerPassword", "text", 64),
    "pve": ("gus", SS, "serverPVE", "bool", None),
    "xp": ("gus", SS, "XPMultiplier", "float", 1000),
    "taming": ("gus", SS, "TamingSpeedMultiplier", "float", 1000),
    "harvest": ("gus", SS, "HarvestAmountMultiplier", "float", 1000),
    "difficulty": ("gus", SS, "OverrideOfficialDifficulty", "float", 100),
    "dayCycle": ("gus", SS, "DayCycleSpeedScale", "float", 100),
    "dayTime": ("gus", SS, "DayTimeSpeedScale", "float", 100),
    "nightTime": ("gus", SS, "NightTimeSpeedScale", "float", 100),
    "stackSize": ("gus", SS, "ItemStackSizeMultiplier", "float", 1000),
    "thirdPerson": ("gus", SS, "AllowThirdPersonPlayer", "bool", None),
    "crosshair": ("gus", SS, "ServerCrosshair", "bool", None),
    "mapPlayerLocation": ("gus", SS, "ShowMapPlayerLocation", "bool", None),
    "flyerCarry": ("gus", SS, "AllowFlyerCarryPvE", "bool", None),
    "noDecayPvE": ("gus", SS, "DisableStructureDecayPvE", "bool", None),
    "offlinePvP": ("gus", SS, "PreventOfflinePvP", "bool", None),
    "autoSaveMinutes": ("gus", SS, "AutoSavePeriodMinutes", "float", 600),
    "kickIdleSeconds": ("gus", SS, "KickIdlePlayersPeriod", "float", 86400),
    "maxTamedDinos": ("gus", SS, "MaxTamedDinos", "int", 100000),
    "foodDrain": ("gus", SS, "PlayerCharacterFoodDrainMultiplier", "float", 100),
    "waterDrain": ("gus", SS, "PlayerCharacterWaterDrainMultiplier", "float", 100),
    "playerDamage": ("gus", SS, "PlayerDamageMultiplier", "float", 100),
    "dinoDamage": ("gus", SS, "DinoDamageMultiplier", "float", 100),
    "structureDamage": ("gus", SS, "StructureDamageMultiplier", "float", 100),
    "dinoCount": ("gus", SS, "DinoCountMultiplier", "float", 100),
    "motd": ("gus", "MessageOfTheDay", "Message", "text", 500),
    "mating": ("game", MODE, "MatingIntervalMultiplier", "float", 100),
    "eggHatch": ("game", MODE, "EggHatchSpeedMultiplier", "float", 1000),
    "babyMature": ("game", MODE, "BabyMatureSpeedMultiplier", "float", 1000),
    "cuddleInterval": ("game", MODE, "BabyCuddleIntervalMultiplier", "float", 100),
    "imprintScale": ("game", MODE, "BabyImprintingStatScaleMultiplier", "float", 100),
    "cropGrowth": ("game", MODE, "CropGrowthSpeedMultiplier", "float", 1000),
    "unlimitedRespecs": ("game", MODE, "bAllowUnlimitedRespecs", "bool", None),
}
ENV = {
    "ASE_SESSION_NAME": "sessionName", "ASE_MAX_PLAYERS": "maxPlayers", "ASE_PASSWORD": "password",
    "ASE_PVE": "pve", "ASE_XP": "xp", "ASE_TAMING": "taming", "ASE_HARVEST": "harvest",
    "ASE_DIFFICULTY": "difficulty",
}
SECRETS = ("password",)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path, data, indent=2):
    write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


def out(data):
    print(json.dumps(data, ensure_ascii=False))


def fail(message, code=1):
    out({"ok": False, "error": message})
    sys.exit(code)


def load(path):
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def workshop_ids(value):
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", str(value or ""))
    ids = []
    for item in items:
        m = re.search(r"[?&]id=(\d{1,20})", str(item)) or re.fullmatch(r"(\d{1,20})", str(item).strip())
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def coerce(name, value):
    _, _, _, kind, limit = SETTINGS[name]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} must be true or false")
    if kind in ("int", "float"):
        try:
            n = float(str(value).strip())
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
        if not 0 <= n <= limit or (name == "maxPlayers" and n < 1):
            raise ValueError(f"{name} must be from {1 if name == 'maxPlayers' else 0} to {limit}")
        return int(n) if kind == "int" else n
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    if name in ("password", "sessionName") and "?" in text:
        raise ValueError(f"{name} cannot contain ?")
    return text[:limit]


def effective(settings=None):
    settings = load(SETTINGS_FILE) if settings is None else settings
    values, sources = {}, {}
    env_for = {v: k for k, v in ENV.items()}
    for name in SETTINGS:
        env = env_for.get(name)
        if env and str(os.environ.get(env, "")).strip() != "":
            try:
                values[name], sources[name] = coerce(name, os.environ[env]), "env"
                continue
            except ValueError:
                pass
        if name in settings:
            values[name], sources[name] = settings[name], "panel"
    return values, sources


# ------------------------------------------------------------------------------------------------
# ini files: our keys set, every other line kept (the server writes these files too)
# ------------------------------------------------------------------------------------------------

def ini_value(kind, value):
    if kind == "bool":
        return "True" if value else "False"
    if kind == "float":
        return f"{float(value):.6f}"
    return str(value)


def set_ini(text, changes):
    """changes: {section: {key: value}}. Replaces a key's line in its section (case-insensitively,
    as Unreal reads it), appends it to the section, or adds the section."""
    lines = text.splitlines()
    out_lines, section, done = [], None, set()

    def flush(sec):
        for key, value in changes.get(sec, {}).items():
            if (sec, key.lower()) not in done:
                out_lines.append(f"{key}={value}")
                done.add((sec, key.lower()))

    for line in lines:
        m = re.match(r"^\s*\[(.+)\]\s*$", line)
        if m:
            if section is not None:
                flush(section)
            section = m.group(1)
            out_lines.append(line)
            continue
        if section in changes and "=" in line:
            key = line.split("=", 1)[0].strip()
            match = next((k for k in changes[section] if k.lower() == key.lower()), None)
            if match:
                if (section, match.lower()) not in done:
                    out_lines.append(f"{match}={changes[section][match]}")
                    done.add((section, match.lower()))
                continue
        out_lines.append(line)
    if section is not None:
        flush(section)
    for sec, keys in changes.items():
        pending = {k: v for k, v in keys.items() if (sec, k.lower()) not in done}
        if pending:
            out_lines.append(f"[{sec}]")
            out_lines += [f"{k}={v}" for k, v in pending.items()]
            out_lines.append("")
    return "\n".join(out_lines).rstrip("\n") + "\n"


def read_ini(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def ini_get(text, section, key):
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*\[(.+)\]\s*$", line)
        if m:
            current = m.group(1)
        elif current == section and "=" in line and line.split("=", 1)[0].strip().lower() == key.lower():
            return line.split("=", 1)[1].strip()
    return None


def admin_password():
    if str(os.environ.get("ASE_ADMIN_PASSWORD", "")).strip():
        return os.environ["ASE_ADMIN_PASSWORD"]
    try:
        with open(ADMIN_SECRET, encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    write_text(ADMIN_SECRET, value + "\n", mode=0o600)
    return value


def mod_map_id():
    """A map from the Workshop (the panel's Map tab), by its id; None for an official map."""
    value = str(load(SETTINGS_FILE).get("modMap") or "").strip()
    return value if re.fullmatch(r"\d{1,20}", value) else None


def map_name():
    """THE ONE EXCEPTION to env-first: ASE_MAP is the FIRST map. Once the panel's Map tab picked
    one, that is the map (the spec always carries ASE_MAP, so env-first would make the tab do
    nothing). Each map keeps its own save folder, so switching back finds that world again."""
    value = (load(SETTINGS_FILE).get("map") or os.environ.get("ASE_MAP") or "TheIsland").strip()
    return value if value in MAPS else "TheIsland"


def map_source():
    if mod_map_id():
        return "panel"
    if load(SETTINGS_FILE).get("map") in MAPS:
        return "panel"
    return "env" if os.environ.get("ASE_MAP") in MAPS else "default"


def save_folder():
    """The current map's save folder name under SavedArks: the map's id, or Mod_<id>."""
    mid = mod_map_id()
    return f"Mod_{mid}" if mid else map_name()


def migrate_saves():
    """Image 1.0.0/1.0.1 kept every map in SavedArks itself, with ONE set of players and tribes
    for all of them. Once: each map's world (and ARK's copies of it) into SavedArks/<Map>/, the
    players and tribes COPIED beside every world (each map starts with the characters it had),
    then the shared originals kept aside in SavedArks/.flux-premigration/. Returns what moved."""
    os.makedirs(SAVED_ARKS, exist_ok=True)
    top = [n for n in os.listdir(SAVED_ARKS) if os.path.isfile(os.path.join(SAVED_ARKS, n))]
    worlds = sorted({n[:-4] for n in top if n.endswith(".ark") and not DATED_RE.match(n)})
    shared = [n for n in top if n.endswith((".arkprofile", ".arktribe", ".profilebak", ".tribebak"))]
    # Nothing flat left (a server on 1.0.2 already, or a fresh one): nothing to do. A flat backup
    # from an older image, restored, is moved the same way on the next start.
    if not worlds and not shared:
        return []
    moved = []
    targets = set(worlds) | {save_folder()}
    for m in sorted(targets):
        folder = os.path.join(SAVED_ARKS, m)
        os.makedirs(folder, exist_ok=True)
        for n in top:
            belongs = n == f"{m}.ark" or (n.startswith(f"{m}_") and (n.endswith(".bak") or DATED_RE.match(n)))
            if belongs and os.path.exists(os.path.join(SAVED_ARKS, n)):
                os.replace(os.path.join(SAVED_ARKS, n), os.path.join(folder, n))
                moved.append(f"{m}/{n}")
        for n in shared:
            dst = os.path.join(folder, n)
            if not os.path.exists(dst):
                shutil.copy2(os.path.join(SAVED_ARKS, n), dst)
    if shared:
        aside = os.path.join(SAVED_ARKS, ".flux-premigration")
        os.makedirs(aside, exist_ok=True)
        for n in shared:
            os.replace(os.path.join(SAVED_ARKS, n), os.path.join(aside, n))
    return moved or shared


def dated_copies(folder):
    """ARK's dated copies of the worlds in a save folder, newest first, by file time."""
    try:
        names = [n for n in os.listdir(folder) if DATED_RE.match(n)]
    except OSError:
        return []
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)


def prune_dated(keep=None):
    """Keeps the newest FLUX_DATED_KEEP dated copies per map (ARK never deletes them, and each is
    a whole world: they fill the disk on a long-running server). Returns the files removed."""
    keep = DATED_KEEP if keep is None else keep
    removed = []
    folders = [SAVED_ARKS] + [os.path.join(SAVED_ARKS, d) for d in (os.listdir(SAVED_ARKS) if os.path.isdir(SAVED_ARKS) else [])
                              if os.path.isdir(os.path.join(SAVED_ARKS, d)) and not d.startswith(".")]
    for folder in folders:
        by_map = {}
        for n in dated_copies(folder):
            by_map.setdefault(DATED_RE.match(n).group("map"), []).append(n)
        for names in by_map.values():
            for n in names[keep:]:
                try:
                    os.remove(os.path.join(folder, n))
                    removed.append(os.path.relpath(os.path.join(folder, n), SAVED_ARKS))
                except OSError:
                    pass
    return removed


def prune_game_logs(keep=10):
    """ARK keeps every ShooterGame-backup-*.log it rotates at start: the newest `keep` stay."""
    folder = os.path.join(SAVED, "Logs")
    try:
        names = sorted((n for n in os.listdir(folder) if n.startswith("ShooterGame-backup-")), reverse=True)
    except OSError:
        return
    for n in names[keep:]:
        try:
            os.remove(os.path.join(folder, n))
        except OSError:
            pass


def mask(text):
    """A launch line or log line with the passwords in it, safe to print."""
    return re.sub(r"((?:ServerAdminPassword|ServerPassword)=)[^?\s\"]*", r"\1***", str(text))


def cmd_apply():
    moved = migrate_saves()
    if moved:
        print(f"# saves: moved to one folder per map ({len(moved)} file(s)); players and tribes copied into each map's folder, the shared originals kept in SavedArks/.flux-premigration")
    removed = prune_dated()
    if removed:
        print(f"# saves: removed {len(removed)} old dated world copies (keeping {DATED_KEEP} per map)")
    prune_game_logs()
    os.makedirs(os.path.join(SAVED_ARKS, save_folder()), exist_ok=True)
    values, sources = effective()
    changes = {"gus": {}, "game": {}}
    for name, value in values.items():
        file, section, key, kind, _ = SETTINGS[name]
        changes[file].setdefault(section, {})[key] = ini_value(kind, value)
    mods = active_mods()
    mid = mod_map_id()
    if mid and mid not in mods:
        mods = mods + [mid]
    # RCON on (inside only), the admin password, the mods: always ours.
    changes["gus"].setdefault(SS, {}).update({"RCONEnabled": "True", "RCONPort": str(RCON_PORT),
                                             "ServerAdminPassword": admin_password(),
                                             "ActiveMods": ",".join(mods)})
    if "password" not in values:
        changes["gus"][SS]["ServerPassword"] = ini_get(read_ini(GUS), SS, "ServerPassword") or ""
    write_text(GUS, set_ini(read_ini(GUS), changes["gus"]))
    write_text(GAME_INI, set_ini(read_ini(GAME_INI), changes["game"]) if changes["game"] else (read_ini(GAME_INI) or "\n"))
    state = read_json(STATE_FILE, {}) or {}
    state.update({"applied": int(time.time()) + 1, "map": map_name(), "modMap": mid, "saveFolder": save_folder()})
    write_json(STATE_FILE, state, indent=None)
    from_env = [n for n, s in sources.items() if s == "env"]
    from_panel = [n for n, s in sources.items() if s == "panel"]
    where = f"mod map {mid}" if mid else f"map {map_name()} ({MAPS[map_name()][0]})"
    print(f"# {where}, saves in SavedArks/{save_folder()}, mods: {','.join(mods) or '-'}; settings from env: {', '.join(from_env) or '-'}; from the panel: {', '.join(from_panel) or '-'}")
    # The URL the launch line starts with: the map, the ports, the save folder, and what ARK does
    # NOT take from a GameUserSettings.ini written before its first start (measured: it rewrote
    # the file with "ARK #546145", 70 players and no admin password, so RCON never opened). On the
    # URL these win, and ARK writes them back into the ini itself. AltSaveDirectoryName is a URL
    # option: as a dash argument the server ignores it (measured).
    port = os.environ.get("ASE_PORT", "7777")
    query = os.environ.get("ASE_QUERY_PORT", "27015")
    gus_now = read_ini(GUS)
    session = values.get("sessionName") or ini_get(gus_now, "SessionSettings", "SessionName") or "ARK on Flux"
    players = values.get("maxPlayers") or ini_get(gus_now, SESSION, "MaxPlayers") or 70
    password = values.get("password") if "password" in values else (ini_get(gus_now, SS, "ServerPassword") or "")
    # A Workshop map: the map name on the URL is ignored, -MapModID chooses the map.
    url = ["TheIsland" if mid else map_name(), "listen", f"SessionName={session}", f"Port={port}", f"QueryPort={query}",
           f"MaxPlayers={int(float(players))}", "RCONEnabled=True", f"RCONPort={RCON_PORT}",
           f"ServerAdminPassword={admin_password()}", f"AltSaveDirectoryName=SavedArks/{save_folder()}"]
    if password:
        url.append(f"ServerPassword={password}")
    print("?".join(url))
    if mid:
        print(f"ARG -MapModID={mid}")
    # Transfers between THIS server's maps (obelisk upload/download): a cluster of one server, its
    # folder on the data volume. Off unless the owner turns it on in the Map tab.
    if load(SETTINGS_FILE).get("mapTransfers") is True:
        print(f"ARG -clusterid=flux{os.environ.get('FLUX_CLUSTER_SUFFIX', '')}")
        print(f"ARG -ClusterDirOverride={os.path.join(SAVED, 'clusters')}")


# ------------------------------------------------------------------------------------------------
# Mods: SteamCMD's Workshop download (anonymous works for 346110), unpacked the way the game's own
# Steam client does: every .z is a chunked zlib file; <id>.mod is written from mod.info and
# modmeta.info (arkmanager's doExtractMod, ported).
# ------------------------------------------------------------------------------------------------

def active_mods():
    return workshop_ids(os.environ.get("ASE_MODS", "")) + [i for i in workshop_ids(load(MODS_FILE).get("mods", [])) if i not in workshop_ids(os.environ.get("ASE_MODS", ""))]


def unpack_z(src, dst):
    """A UE4 compressed file: signature C1 83 2A 9E 00000000, chunk size, compressed and
    uncompressed totals (int64 each), then (compressed, uncompressed) int64 pairs, then the chunks."""
    with open(src, "rb") as f:
        if f.read(8) != b"\xC1\x83\x2A\x9E\x00\x00\x00\x00":
            raise ValueError(f"bad signature in {src}")
        _chunk, comp_total, _uncomp_total = struct.unpack("<qqq", f.read(24))
        sizes, used = [], 0
        while used < comp_total:
            comp, _uncomp = struct.unpack("<qq", f.read(16))
            sizes.append(comp)
            used += comp
        with open(dst + ".part", "wb") as o:
            for comp in sizes:
                o.write(zlib.decompress(f.read(comp)))
    os.replace(dst + ".part", dst)


def write_mod_file(mod_dir, mod_id, title):
    """<id>.mod beside the mod's folder: the id, the name, the path the server loads it from, the
    maps the mod adds (from mod.info), a marker, then modmeta.info as it is."""
    info = open(os.path.join(mod_dir, "mod.info"), "rb").read()
    name_len = struct.unpack_from("<I", info, 0)[0]
    map_name_in = info[4:4 + name_len - 1].decode("utf-8", "replace")
    count = struct.unpack_from("<I", info, 4 + name_len)[0]
    pos = 8 + name_len
    maps = []
    for _ in range(count):
        length = struct.unpack_from("<I", info, pos)[0]
        maps.append(info[pos + 4:pos + 4 + length])
        pos += 4 + length
    name = ((title or map_name_in) + "\x00").encode("utf-8")
    path = f"../../../ShooterGame/Content/Mods/{mod_id}\x00".encode()
    data = struct.pack("<QI", int(mod_id), len(name)) + name + struct.pack("<I", len(path)) + path + struct.pack("<I", count)
    for m in maps:
        data += struct.pack("<I", len(m)) + m
    data += b"\x33\xFF\x22\xFF\x02\x00\x00\x00\x01"
    meta = os.path.join(mod_dir, "modmeta.info")
    data += open(meta, "rb").read() if os.path.exists(meta) else b"\x01\x00\x00\x00\x08\x00\x00\x00ModType\x00\x02\x00\x00\x001\x00"
    with open(mod_dir + ".mod", "wb") as f:
        f.write(data)


def workshop_installed():
    """SteamCMD's own record of the Workshop items it downloaded (appworkshop_346110.acf):
    {id: timeupdated}. The time is the Workshop's for the version actually on disk."""
    try:
        text = open(WORKSHOP_ACF, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    m = re.search(r'"WorkshopItemsInstalled"\s*\{(.*?)\n\t\}', text, re.S)
    body = m.group(1) if m else text
    return {i: int(t) for i, t in re.findall(r'"(\d+)"\s*\{[^{}]*?"timeupdated"\s+"(\d+)"', body)}


def installed_version(mod_id):
    """The Workshop time of the version unpacked in Mods/<id>, or 0."""
    try:
        return int(open(os.path.join(MODS_DIR, mod_id, "__flux_version__")).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def install_mod(mod_id, title=None, version=None):
    """Unpacks Workshop item `mod_id` into Mods/<id> + <id>.mod. `version`: the Workshop time of
    the download (SteamCMD's acf), stamped so an unchanged mod is not unpacked again. False when
    it was already up to date."""
    src = os.path.join(WORKSHOP_CONTENT, mod_id)
    dst = os.path.join(MODS_DIR, mod_id)
    stamp = os.path.join(dst, "__flux_version__")
    if version is None and os.path.isdir(src):
        version = int(max([os.path.getmtime(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs] or [0]))
    installed = open(stamp).read().strip() if os.path.exists(stamp) else None
    if installed and os.path.exists(dst + ".mod") and (version is None or installed == str(int(version))):
        return False
    # The Windows branch, on Linux too: what the game's own client uses, and what arkmanager uses
    # ("Linux mods are known to cause the server to crash").
    branch = os.path.join(src, "WindowsNoEditor")
    if os.path.exists(os.path.join(branch, "mod.info")):
        src = branch
    if not os.path.exists(os.path.join(src, "mod.info")):
        raise ValueError("no mod.info: not an ARK mod, or not downloaded")
    version = str(int(version or 0))
    staging = dst + ".flux-new"
    shutil.rmtree(staging, ignore_errors=True)
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(staging, rel), exist_ok=True)
        for f in files:
            if f.endswith(".z.uncompressed_size"):
                continue
            target = os.path.join(staging, rel, f[:-2] if f.endswith(".z") else f)
            if f.endswith(".z"):
                unpack_z(os.path.join(root, f), target)
            else:
                shutil.copy2(os.path.join(root, f), target)
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(staging, dst)
    write_mod_file(dst, mod_id, title)
    with open(stamp, "w") as f:
        f.write(version + "\n")
    # The download is not needed once unpacked (it doubled the disk a mod takes). SteamCMD
    # downloads it again only when the Workshop has a newer version (sync_mods asks first).
    shutil.rmtree(os.path.join(WORKSHOP_CONTENT, mod_id), ignore_errors=True)
    return True


def mod_titles(ids):
    if not ids:
        return {}
    form = "&".join([f"itemcount={len(ids)}"] + [f"publishedfileids%5B{i}%5D={x}" for i, x in enumerate(ids)])
    try:
        req = urllib.request.Request("https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/", data=form.encode())
        details = json.loads(urllib.request.urlopen(req, timeout=20).read())["response"]["publishedfiledetails"]
        return {d["publishedfileid"]: d for d in details}
    except Exception:
        return {}


def all_mod_ids():
    ids = active_mods()
    mid = mod_map_id()
    return ids + ([mid] if mid and mid not in ids else [])


def cmd_sync_mods():
    """Before every start. Asks the Workshop what each mod's latest version is, downloads only the
    mods that are missing or older (SteamCMD, anonymous), unpacks them, and records per mod the
    version installed, the latest known, and a failed download (retried after 6 h, so a broken
    download does not restart the server again and again)."""
    sys.stdout.reconfigure(line_buffering=True)
    ids = all_mod_ids()
    os.makedirs(MODS_DIR, exist_ok=True)
    old = {m.get("id"): m for m in (read_json(MODS_STATE, {}) or {}).get("mods", [])}
    state = {"mods": [], "at": int(time.time())}
    if not ids:
        write_json(MODS_STATE, state)
        return
    details = mod_titles(ids)
    now = int(time.time())
    wanted = []
    for i in ids:
        latest = int((details.get(i) or {}).get("time_updated") or 0)
        have = installed_version(i)
        if not have or (latest and latest > have):
            prev = old.get(i) or {}
            if prev.get("failedTarget") == latest and now - int(prev.get("failedAt") or 0) < FAILED_RETRY_SECONDS and have:
                continue
            wanted.append(i)
    log_tail = ""
    if wanted:
        print(f"# Workshop: downloading {len(wanted)} of {len(ids)} mod(s)")
        cmd = ["steamcmd", "+force_install_dir", WORKSHOP, "+login", "anonymous"]
        for i in wanted:
            cmd += ["+workshop_download_item", "346110", i]
        for attempt in range(3):
            r = subprocess.run(cmd + ["+quit"], capture_output=True, text=True)
            log_tail = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if l.strip())[-600:]
            if all(os.path.isdir(os.path.join(WORKSHOP_CONTENT, i)) for i in wanted):
                break
            print(f"# WARN steamcmd attempt {attempt + 1}/3 left {sum(not os.path.isdir(os.path.join(WORKSHOP_CONTENT, i)) for i in wanted)} mod(s) missing")
            time.sleep(5)
    acf = workshop_installed()
    for i in ids:
        d = details.get(i) or {}
        latest = int(d.get("time_updated") or 0)
        entry = {"id": i, "title": d.get("title") or (old.get(i) or {}).get("title"), "size": d.get("file_size"),
                 "latest": latest or None, "mapMod": i == mod_map_id()}
        if i in wanted and os.path.isdir(os.path.join(WORKSHOP_CONTENT, i)):
            try:
                if install_mod(i, entry["title"], version=acf.get(i) or latest or None):
                    print(f"# mod {i} ({entry['title'] or '?'}): unpacked")
            except (OSError, ValueError, zlib.error, struct.error) as e:
                entry["error"] = str(e)
                print(f"# WARN mod {i}: {e}")
        elif i in wanted:
            entry["error"] = "the download failed"
            print(f"# WARN mod {i}: the download failed; steamcmd said: {log_tail.splitlines()[-1] if log_tail else 'nothing'}")
        have = installed_version(i)
        entry["updated"] = have or None
        entry["installed"] = bool(have) and os.path.exists(os.path.join(MODS_DIR, f"{i}.mod"))
        if entry.get("error") and latest:
            entry.update({"failedTarget": latest, "failedAt": now})
        elif (old.get(i) or {}).get("failedTarget") and not (latest and latest > have):
            pass
        elif (old.get(i) or {}).get("failedTarget") == latest and latest > have:
            entry.update({"failedTarget": latest, "failedAt": (old.get(i) or {}).get("failedAt")})
        state["mods"].append(entry)
    write_json(MODS_STATE, state)


def cmd_mods_outdated():
    """For the supervisor: one line "<id> <title>" per mod with a newer version on the Workshop
    that has not failed to download in the last 6 h."""
    state = read_json(MODS_STATE, {}) or {}
    mods = {m["id"]: m for m in state.get("mods", []) if m.get("installed")}
    now = int(time.time())
    for i, d in mod_titles(list(mods)).items():
        latest = int(d.get("time_updated") or 0)
        m = mods.get(i, {})
        if latest > int(m.get("updated") or 0):
            if m.get("failedTarget") == latest and now - int(m.get("failedAt") or 0) < FAILED_RETRY_SECONDS:
                continue
            print(i, d.get("title", ""))


# ------------------------------------------------------------------------------------------------
# RCON (Source RCON on 127.0.0.1)
# ------------------------------------------------------------------------------------------------

def rcon(command, timeout=8.0):
    def packet(req_id, kind, body):
        payload = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def read(s):
        size = struct.unpack("<i", recv_exact(s, 4))[0]
        data = recv_exact(s, size)
        req_id, kind = struct.unpack("<ii", data[:8])
        return req_id, kind, data[8:-2].decode("utf-8", "replace")

    def recv_exact(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise OSError("RCON closed the connection")
            buf += chunk
        return buf

    with socket.create_connection(("127.0.0.1", RCON_PORT), timeout=timeout) as s:
        s.sendall(packet(1, 3, admin_password()))
        while True:
            req_id, kind, _ = read(s)
            if kind == 2:
                if req_id == -1:
                    raise RuntimeError("RCON refused the admin password")
                break
        s.sendall(packet(2, 2, command))
        _, _, body = read(s)
        return body


def parse_players(text):
    """ListPlayers: "No Players Connected" or "0. Name, <id>" per player. The id is the SteamID64
    for Steam players and the 32-hex Epic (EOS) account id for Epic crossplay players."""
    players = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\.\s*(.*?),\s*(\d{17}|[0-9a-fA-F]{32})\s*$", line)
        if m:
            pid = m.group(3)
            players.append({"index": int(m.group(1)), "name": m.group(2), "id": pid,
                            "platform": "steam" if pid.isdigit() else "epic"})
    return players


def cmd_rcon(*words):
    command = re.sub(r"[\x00-\x1f\x7f]", " ", " ".join(words)).strip()
    if not command:
        fail("no command", 64)
    # ARK's console runs several commands joined with "|": one command per call, always (it would
    # otherwise carry a DoExit past the panel's checks inside a chat message).
    if "|" in command:
        fail("one command at a time: | is not allowed", 2)
    try:
        reply = rcon(command)
    except (OSError, RuntimeError) as e:
        fail(f"RCON: {e}", 3)
    result = {"ok": True, "command": command, "reply": reply.strip()}
    if command.lower() == "listplayers":
        result["players"] = parse_players(reply)
    out(result)


# ------------------------------------------------------------------------------------------------
# State, settings, restart
# ------------------------------------------------------------------------------------------------

def cmd_state(state, message=None):
    data = read_json(STATE_FILE, {}) or {}
    if data.get("state") != state:
        data["since"] = int(time.time())
    elif message is None:
        # The same state said again without a word (the loop's own "stopped" after the stop
        # handler said whether the save was confirmed): keep what was said.
        message = data.get("message")
    data.update({"state": state, "message": message or None, "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")})
    write_json(STATE_FILE, data, indent=None)


def installed_build():
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', open(os.path.join(SERVER_DIR, "steamapps", "appmanifest_376030.acf"), encoding="utf-8", errors="replace").read())
        return m.group(1) if m else None
    except OSError:
        return None


def game_version():
    try:
        return open(os.path.join(SERVER_DIR, "version.txt"), encoding="utf-8", errors="replace").read().strip() or None
    except OSError:
        return None


def cmd_status():
    state = read_json(STATE_FILE, {}) or {}
    players, answering = [], False
    if state.get("state") == "running":
        try:
            players, answering = parse_players(rcon("ListPlayers", timeout=5)), True
        except (OSError, RuntimeError):
            pass
    gus = read_ini(GUS)
    out({"state": state.get("state", "stopped"), "message": state.get("message"), "since": state.get("since"),
         "image": state.get("image"), "build": installed_build(), "version": game_version(),
         "map": state.get("map") or map_name(), "mapName": (f"Workshop map {state.get('modMap')}" if state.get("modMap") else MAPS.get(state.get("map") or map_name(), ("?",))[0]),
         "modMap": state.get("modMap"), "saveFolder": state.get("saveFolder") or save_folder(),
         "name": ini_get(gus, "SessionSettings", "SessionName"), "players": players,
         "max_players": int(float(ini_get(gus, SESSION, "MaxPlayers") or 70)), "rcon": answering,
         "mods": [m for m in (read_json(MODS_STATE, {}) or {}).get("mods", [])],
         "pending": pending(state), "lastRestore": read_json(RESTORE_RESULT)})


def pending(state):
    if os.path.exists(RESTORE_PENDING):
        return True
    for p in (SETTINGS_FILE, MODS_FILE):
        try:
            if os.path.getmtime(p) > (state.get("applied") or 0):
                return True
        except OSError:
            pass
    return False


def cmd_pending():
    """Exit 0 when settings, mods or a restore were saved after this start applied its own: the
    supervisor restarts onto them once the server is up (saved while it was still starting)."""
    sys.exit(0 if pending(read_json(STATE_FILE, {}) or {}) else 1)


def cmd_settings():
    values, sources = effective()
    gus, game = read_ini(GUS), read_ini(GAME_INI)
    current = {}
    for name, (file, section, key, kind, _) in SETTINGS.items():
        raw = ini_get(gus if file == "gus" else game, section, key)
        current[name] = raw
    shown = {k: ("(set)" if v else "") if k in SECRETS else v for k, v in values.items()}
    out({"values": shown, "sources": sources, "file": {k: (("(set)" if v else "") if k in SECRETS else v) for k, v in current.items()},
         "map": map_name(), "mapSource": map_source(), "maps": {k: {"name": v[0], "dlc": v[1]} for k, v in MAPS.items()},
         "modMap": mod_map_id(), "mapTransfers": load(SETTINGS_FILE).get("mapTransfers") is True,
         "joinPassword": values.get("password") if "password" in values else (ini_get(gus, SS, "ServerPassword") or ""),
         "adminPassword": admin_password(), "env": {k: v for k, v in ENV.items() if k in os.environ}})


def cmd_set_settings(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(incoming, dict):
        fail("expected a JSON object")
    settings = load(SETTINGS_FILE)
    errors = []
    for name, value in incoming.items():
        if name == "map":
            if value not in MAPS:
                errors.append(f"map must be one of {', '.join(MAPS)}")
            else:
                settings["map"] = value
                settings.pop("modMap", None)
            continue
        if name == "modMap":
            ids = workshop_ids([value]) if value else []
            if value and not ids:
                errors.append("modMap must be a Workshop ID or link")
            elif ids:
                settings["modMap"] = ids[0]
            else:
                settings.pop("modMap", None)
            continue
        if name == "mapTransfers":
            settings["mapTransfers"] = value is True or str(value).lower() in ("1", "true", "yes", "on")
            continue
        if name not in SETTINGS:
            errors.append(f"unknown setting {name}")
            continue
        if value is None:
            settings.pop(name, None)
            continue
        try:
            settings[name] = coerce(name, value)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        fail("; ".join(errors), 2)
    write_json(SETTINGS_FILE, settings)
    out({"ok": True, "settings": {k: v for k, v in settings.items() if k not in SECRETS}, "appliesOn": "restart"})


def cmd_mods():
    out({"mods": (read_json(MODS_STATE, {}) or {}).get("mods", []), "panel": workshop_ids(load(MODS_FILE).get("mods", [])),
         "env": workshop_ids(os.environ.get("ASE_MODS", "")), "active": active_mods(), "modMap": mod_map_id(),
         "disk": {"mods": dir_size(MODS_DIR), "workshop": dir_size(WORKSHOP), "serverVolume": disk_usage()["serverVolume"]}})


def cmd_set_mods(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    ids = workshop_ids((incoming or {}).get("mods", []))
    write_json(MODS_FILE, {"mods": ids})
    out({"ok": True, "mods": ids, "appliesOn": "restart"})


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_usage():
    """What the saves, the mods and the game take, and what is left on each volume."""
    def free(path):
        try:
            u = shutil.disk_usage(path)
            return {"total": u.total, "free": u.free}
        except OSError:
            return None
    return {"saves": dir_size(SAVED_ARKS), "backups": dir_size(BACKUP_DIR), "mods": dir_size(MODS_DIR),
            "workshop": dir_size(WORKSHOP), "dataVolume": free(DATA_DIR), "serverVolume": free(SERVER_DIR)}


def cmd_saves():
    """Every map's save folder (SavedArks/<Map>/): its world, its players (.arkprofile) and tribes
    (.arktribe), ARK's .bak and dated copies; a folder left from before 1.0.2 (`legacy`); disk."""
    current = save_folder()
    maps = []
    if os.path.isdir(SAVED_ARKS):
        for d in sorted(os.listdir(SAVED_ARKS)):
            folder = os.path.join(SAVED_ARKS, d)
            if d.startswith(".") or not os.path.isdir(folder) or d == "clusters":
                continue
            world = os.path.join(folder, f"TheIsland.ark" if d.startswith("Mod_") else f"{d}.ark")
            arks = [n for n in os.listdir(folder) if n.endswith(".ark") and not DATED_RE.match(n)]
            if not os.path.exists(world) and arks:
                world = os.path.join(folder, arks[0])
            names = os.listdir(folder)
            entry = {"folder": d, "map": d, "name": (f"Workshop map {d[4:]}" if d.startswith("Mod_") else MAPS.get(d, (d,))[0]),
                     "active": d == current, "profiles": sum(n.endswith(".arkprofile") for n in names),
                     "tribes": sum(n.endswith(".arktribe") for n in names), "dated": len(dated_copies(folder)),
                     "gameBackups": sorted(n for n in names if n.endswith(".bak")), "size": dir_size(folder)}
            if os.path.exists(world):
                st = os.stat(world)
                entry.update({"world": os.path.basename(world), "worldSize": st.st_size, "modified": int(st.st_mtime)})
            maps.append(entry)
    legacy = [n for n in (os.listdir(SAVED_ARKS) if os.path.isdir(SAVED_ARKS) else []) if n.endswith(".ark") and os.path.isfile(os.path.join(SAVED_ARKS, n))]
    out({"maps": maps, "worlds": [m for m in maps if m.get("world")], "current": current, "map": map_name(), "modMap": mod_map_id(),
         "legacy": legacy, "backups": list_backups(), "path": "Saved/SavedArks", "backupPath": "flux/backups",
         "disk": disk_usage(), "datedKeep": DATED_KEEP})


# ------------------------------------------------------------------------------------------------
# Backups and restore. The archives are written by the supervisor (flux-entrypoint.sh); a restore
# is applied by it too, before the next start: a command run through exec cannot stop the server
# and swap its files under it.
# ------------------------------------------------------------------------------------------------

BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}-ark(?:-[a-z0-9-]{1,40})?\.tar\.gz$")


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    rows = []
    for name in os.listdir(BACKUP_DIR):
        if not BACKUP_NAME_RE.match(name):
            continue
        st = os.stat(os.path.join(BACKUP_DIR, name))
        reason = name[len("YYYYmmdd-HHMMSS-ark"):-len(".tar.gz")].lstrip("-") or None
        rows.append({"file": name, "size": st.st_size, "modified": int(st.st_mtime), "reason": reason})
    return sorted(rows, key=lambda r: r["file"], reverse=True)


def backups_to_prune(names_newest_first, times, keep, now=None, protect=()):
    """Which backups to delete: all but the newest `keep`, EXCEPT the newest one older than a day
    and the newest one older than a week (a crash loop's backups can never push every good one
    out), and those in `protect` (the one a pending restore names: the safety backup made before
    it would otherwise push an old one out right before it is read). `times`: {name: epoch}."""
    now = time.time() if now is None else now
    protected = set(names_newest_first[:keep]) | set(protect)
    for age in (86400, 7 * 86400):
        older = [n for n in names_newest_first if now - times[n] >= age]
        if older:
            protected.add(older[0])
    return [n for n in names_newest_first if n not in protected]


def cmd_backups_prune(keep):
    rows = list_backups()
    names = [r["file"] for r in rows]
    pend = (read_json(RESTORE_PENDING) or {}).get("file")
    for n in backups_to_prune(names, {r["file"]: r["modified"] for r in rows}, int(keep), protect=[pend] if pend else []):
        try:
            os.remove(os.path.join(BACKUP_DIR, n))
            print(f"# pruned {n}")
        except OSError:
            pass


def cmd_backup_due(min_age_minutes):
    """Exit 0 when a start backup is due: the newest backup is older than the minimum age."""
    rows = list_backups()
    if rows and time.time() - rows[0]["modified"] < int(min_age_minutes) * 60:
        sys.exit(1)
    sys.exit(0)


def cmd_backups():
    out({"backups": list_backups(), "path": "flux/backups", "pending": read_json(RESTORE_PENDING)})


def cmd_restore(name):
    """Asks for a restore: checked here, applied by the supervisor before the next start (a
    safety backup of the current saves first). Restarts the server when it is running."""
    if not BACKUP_NAME_RE.match(name) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        fail("no such backup", 2)
    import tarfile
    try:
        with tarfile.open(os.path.join(BACKUP_DIR, name)) as t:
            members = t.getnames()
    except (OSError, tarfile.TarError):
        fail("that backup cannot be read", 2)
    if not any(m == "Saved" or m.startswith("Saved/") for m in members) or any(m.startswith("/") or ".." in m.split("/") for m in members):
        fail("that archive is not a backup of this server's saves", 2)
    write_json(RESTORE_PENDING, {"file": name, "at": int(time.time())})
    pids = subprocess.run(["pgrep", "-f", r"ShooterGameServer "], capture_output=True, text=True).stdout.split()
    if pids:
        restart_server("requested: restore " + name)
        out({"ok": True, "restoring": name, "restarting": True})
    else:
        out({"ok": True, "restoring": name, "restarting": False, "appliesOn": "start"})


def cmd_restore_apply(safety="ok"):
    """For the supervisor, the server stopped: the pending restore, if any. `safety` is "failed"
    when the safety backup of the current saves could not be made: then nothing is touched, since
    the current saves would have no copy. Prints what it did and leaves the outcome in
    restore-result.json (`flux-ase status` lastRestore), which the panel reads."""
    pend = read_json(RESTORE_PENDING)
    if not pend:
        return
    name = pend.get("file", "")
    src = os.path.join(BACKUP_DIR, name)

    def done(ok, reason=None):
        write_json(RESTORE_RESULT, {"file": name, "ok": ok, "reason": reason, "at": int(time.time())})
        # Only now: a restore interrupted before this point (the container killed) is tried again.
        try:
            os.remove(RESTORE_PENDING)
        except OSError:
            pass
        print(f"# restored {name}" if ok else f"# WARN restore: {reason}; nothing restored")

    if safety != "ok":
        return done(False, "the safety backup of the current saves could not be made (is the disk full?)")
    if not BACKUP_NAME_RE.match(name) or not os.path.isfile(src):
        return done(False, f"{name} is gone")
    import tarfile
    staging = os.path.join(DATA_DIR, ".flux-restore")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        with tarfile.open(src) as t:
            safe = [m for m in t.getmembers() if not (m.name.startswith("/") or ".." in m.name.split("/")) and (m.isfile() or m.isdir())]
            try:
                t.extractall(staging, members=safe, filter="data")
            except TypeError:  # Python without extraction filters
                t.extractall(staging, members=safe)
    except (OSError, tarfile.TarError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        return done(False, f"{name} could not be unpacked ({e})")
    restored = os.path.join(staging, "Saved")
    if not os.path.isdir(restored):
        shutil.rmtree(staging, ignore_errors=True)
        return done(False, f"{name} holds no Saved folder")
    old = os.path.join(DATA_DIR, f".flux-before-restore-{int(time.time())}")
    if os.path.isdir(SAVED):
        os.replace(SAVED, old)
    os.replace(restored, SAVED)
    # The current saves are in the pre-restore backup (made, or we would not be here); the
    # restored Logs/Config folders are the backup's, ARK's logs of today stay with the old copy.
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    done(True)


# ------------------------------------------------------------------------------------------------
# Game updates that failed to download: not tried again for 6 h (the watcher would otherwise
# restart the server for the same update every half hour while Steam keeps failing).
# ------------------------------------------------------------------------------------------------

def cmd_update_due(installed, public):
    """Exit 0 when the watcher should restart onto `public`; 1 when that same build failed to
    install less than 6 h ago. Records the target either way."""
    failed = read_json(UPDATE_FAILED, {}) or {}
    if failed.get("target") == public and time.time() - int(failed.get("at") or 0) < FAILED_RETRY_SECONDS:
        sys.exit(1)
    write_json(UPDATE_TARGET, {"target": public, "from": installed, "at": int(time.time())})
    sys.exit(0)


def cmd_update_done():
    """After the start's SteamCMD run: when a target was planned and the installed build is not
    it, the update failed: remembered."""
    target = read_json(UPDATE_TARGET)
    if not target:
        return
    try:
        os.remove(UPDATE_TARGET)
    except OSError:
        pass
    if installed_build() != target.get("target"):
        write_json(UPDATE_FAILED, {"target": target.get("target"), "at": int(time.time())})
        print(f"# WARN the update to build {target.get('target')} did not install; not trying it again for 6 h")
    elif os.path.exists(UPDATE_FAILED):
        os.remove(UPDATE_FAILED)


def restart_server(why):
    """Asks the supervisor for an in-place restart: the marker says why, SaveWorld, then SIGINT."""
    import signal
    pids = subprocess.run(["pgrep", "-f", r"ShooterGameServer "], capture_output=True, text=True).stdout.split()
    if not pids:
        return False
    with open(os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested"), "w") as f:
        f.write(why + "\n")
    try:
        rcon("SaveWorld", timeout=30)
    except (OSError, RuntimeError):
        pass
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGINT)
        except OSError:
            pass
    return True


def cmd_restart():
    if not restart_server("requested: from the panel"):
        fail("the server is not running", 3)
    out({"ok": True})


def main(argv):
    commands = {
        "apply": (cmd_apply, (0,)), "sync-mods": (cmd_sync_mods, (0,)), "state": (cmd_state, (1, 2)),
        "admin-password": (lambda: print(admin_password()), (0,)), "status": (cmd_status, (0,)),
        "rcon": (cmd_rcon, tuple(range(1, 30))), "settings": (cmd_settings, (0,)),
        "set-settings": (cmd_set_settings, (1,)), "mods": (cmd_mods, (0,)), "set-mods": (cmd_set_mods, (1,)),
        "saves": (cmd_saves, (0,)), "restart": (cmd_restart, (0,)), "pending": (cmd_pending, (0,)),
        "backups": (cmd_backups, (0,)), "restore": (cmd_restore, (1,)), "restore-apply": (cmd_restore_apply, (0, 1)),
        "backups-prune": (cmd_backups_prune, (1,)), "backup-due": (cmd_backup_due, (1,)),
        "update-due": (cmd_update_due, (2,)), "update-done": (cmd_update_done, (0,)),
        "mods-outdated": (cmd_mods_outdated, (0,)), "mask": (lambda *w: print(mask(" ".join(w))), tuple(range(0, 40))),
    }
    if not argv or argv[0] not in commands or len(argv) - 1 not in commands[argv[0]][1]:
        print(__doc__, file=sys.stderr)
        return 64
    commands[argv[0]][0](*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
