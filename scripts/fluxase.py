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
    flux-ase restart                   save, stop and start again

THE RCON IS NOT PUBLISHED: Source RCON on 127.0.0.1, reached by the hub's exec. The admin password
(also the in-game `enablecheats` password) is the RCON password.

Precedence: an environment variable that is SET is applied on every start; one that is not set
leaves the panel's value, then the file's own.
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
        if env and env in os.environ:
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
    if os.environ.get("ASE_ADMIN_PASSWORD"):
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


def map_name():
    value = (os.environ.get("ASE_MAP") or load(SETTINGS_FILE).get("map") or "TheIsland").strip()
    return value if value in MAPS else "TheIsland"


def cmd_apply():
    values, sources = effective()
    changes = {"gus": {}, "game": {}}
    for name, value in values.items():
        file, section, key, kind, _ = SETTINGS[name]
        changes[file].setdefault(section, {})[key] = ini_value(kind, value)
    mods = active_mods()
    # RCON on (inside only), the admin password, the mods: always ours.
    changes["gus"].setdefault(SS, {}).update({"RCONEnabled": "True", "RCONPort": str(RCON_PORT),
                                             "ServerAdminPassword": admin_password(),
                                             "ActiveMods": ",".join(mods)})
    if "password" not in values:
        changes["gus"][SS]["ServerPassword"] = ini_get(read_ini(GUS), SS, "ServerPassword") or ""
    write_text(GUS, set_ini(read_ini(GUS), changes["gus"]))
    write_text(GAME_INI, set_ini(read_ini(GAME_INI), changes["game"]) if changes["game"] else (read_ini(GAME_INI) or "\n"))
    state = read_json(STATE_FILE, {}) or {}
    state.update({"applied": int(time.time()) + 1, "map": map_name()})
    write_json(STATE_FILE, state, indent=None)
    from_env = [n for n, s in sources.items() if s == "env"]
    from_panel = [n for n, s in sources.items() if s == "panel"]
    print(f"# map {map_name()} ({MAPS[map_name()][0]}), mods: {','.join(mods) or '-'}; settings from env: {', '.join(from_env) or '-'}; from the panel: {', '.join(from_panel) or '-'}")
    # The URL the launch line starts with: the map, the ports, and what ARK does NOT take from a
    # GameUserSettings.ini written before its first start (measured: it rewrote the file with
    # "ARK #546145", 70 players and no admin password, so RCON never opened). On the URL these
    # win, and ARK writes them back into the ini itself.
    port = os.environ.get("ASE_PORT", "7777")
    query = os.environ.get("ASE_QUERY_PORT", "27015")
    gus_now = read_ini(GUS)
    session = values.get("sessionName") or ini_get(gus_now, "SessionSettings", "SessionName") or "ARK on Flux"
    players = values.get("maxPlayers") or ini_get(gus_now, SESSION, "MaxPlayers") or 70
    password = values.get("password") if "password" in values else (ini_get(gus_now, SS, "ServerPassword") or "")
    url = [map_name(), "listen", f"SessionName={session}", f"Port={port}", f"QueryPort={query}",
           f"MaxPlayers={int(float(players))}", "RCONEnabled=True", f"RCONPort={RCON_PORT}",
           f"ServerAdminPassword={admin_password()}"]
    if password:
        url.append(f"ServerPassword={password}")
    print("?".join(url))


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


def install_mod(mod_id, title=None):
    src = os.path.join(WORKSHOP_CONTENT, mod_id)
    # The Windows branch, on Linux too: what the game's own client uses, and what arkmanager uses
    # ("Linux mods are known to cause the server to crash").
    branch = os.path.join(src, "WindowsNoEditor")
    if os.path.exists(os.path.join(branch, "mod.info")):
        src = branch
    if not os.path.exists(os.path.join(src, "mod.info")):
        raise ValueError("no mod.info: not an ARK mod, or not downloaded")
    dst = os.path.join(MODS_DIR, mod_id)
    stamp = os.path.join(dst, "__flux_version__")
    version = str(int(max(os.path.getmtime(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs)))
    if os.path.exists(stamp) and open(stamp).read().strip() == version and os.path.exists(dst + ".mod"):
        return False
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


def cmd_sync_mods():
    sys.stdout.reconfigure(line_buffering=True)
    ids = active_mods()
    os.makedirs(MODS_DIR, exist_ok=True)
    state = {"mods": [], "at": int(time.time())}
    if ids:
        print(f"# Workshop: {len(ids)} mod(s): downloading")
        cmd = ["steamcmd", "+force_install_dir", WORKSHOP, "+login", "anonymous"]
        for i in ids:
            cmd += ["+workshop_download_item", "346110", i]
        for attempt in range(3):
            subprocess.run(cmd + ["+quit"], capture_output=True, text=True)
            if all(os.path.isdir(os.path.join(WORKSHOP_CONTENT, i)) for i in ids):
                break
            time.sleep(5)
        details = mod_titles(ids)
        for i in ids:
            d = details.get(i, {})
            entry = {"id": i, "title": d.get("title"), "size": d.get("file_size"), "updated": d.get("time_updated")}
            try:
                entry["installed"] = True
                if install_mod(i, d.get("title")):
                    print(f"# mod {i} ({d.get('title') or '?'}): unpacked")
            except (OSError, ValueError, zlib.error, struct.error) as e:
                entry.update({"installed": False, "error": str(e)})
                print(f"# WARN mod {i}: {e}")
            state["mods"].append(entry)
    write_json(MODS_STATE, state)


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
    """ListPlayers: "No Players Connected" or "0. Name, 76561198...\\n1. ..."."""
    players = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\.\s*(.*?),\s*(\d{17})\s*$", line)
        if m:
            players.append({"index": int(m.group(1)), "name": m.group(2), "id": m.group(3)})
    return players


def cmd_rcon(*words):
    command = re.sub(r"[\x00-\x1f\x7f]", " ", " ".join(words)).strip()
    if not command:
        fail("no command", 64)
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
         "map": state.get("map") or map_name(), "mapName": MAPS.get(state.get("map") or map_name(), ("?",))[0],
         "name": ini_get(gus, "SessionSettings", "SessionName"), "players": players,
         "max_players": int(float(ini_get(gus, SESSION, "MaxPlayers") or 70)), "rcon": answering,
         "mods": [m for m in (read_json(MODS_STATE, {}) or {}).get("mods", [])],
         "pending": pending(state)})


def pending(state):
    for p in (SETTINGS_FILE, MODS_FILE):
        try:
            if os.path.getmtime(p) > (state.get("applied") or 0):
                return True
        except OSError:
            pass
    return False


def cmd_settings():
    values, sources = effective()
    gus, game = read_ini(GUS), read_ini(GAME_INI)
    current = {}
    for name, (file, section, key, kind, _) in SETTINGS.items():
        raw = ini_get(gus if file == "gus" else game, section, key)
        current[name] = raw
    shown = {k: ("(set)" if v else "") if k in SECRETS else v for k, v in values.items()}
    out({"values": shown, "sources": sources, "file": {k: (("(set)" if v else "") if k in SECRETS else v) for k, v in current.items()},
         "map": map_name(), "maps": {k: {"name": v[0], "dlc": v[1]} for k, v in MAPS.items()},
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
         "env": workshop_ids(os.environ.get("ASE_MODS", "")), "active": active_mods()})


def cmd_set_mods(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    ids = workshop_ids((incoming or {}).get("mods", []))
    write_json(MODS_FILE, {"mods": ids})
    out({"ok": True, "mods": ids, "appliesOn": "restart"})


def cmd_restart():
    pids = subprocess.run(["pgrep", "-f", r"ShooterGameServer "], capture_output=True, text=True).stdout.split()
    if not pids:
        fail("the server is not running", 3)
    with open(os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested"), "w") as f:
        f.write("requested: from the panel\n")
    try:
        rcon("SaveWorld", timeout=30)
    except (OSError, RuntimeError):
        pass
    import signal
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGINT)
        except OSError:
            pass
    out({"ok": True})


def main(argv):
    commands = {
        "apply": (cmd_apply, (0,)), "sync-mods": (cmd_sync_mods, (0,)), "state": (cmd_state, (1, 2)),
        "admin-password": (lambda: print(admin_password()), (0,)), "status": (cmd_status, (0,)),
        "rcon": (cmd_rcon, tuple(range(1, 30))), "settings": (cmd_settings, (0,)),
        "set-settings": (cmd_set_settings, (1,)), "mods": (cmd_mods, (0,)), "set-mods": (cmd_set_mods, (1,)),
        "restart": (cmd_restart, (0,)),
    }
    if not argv or argv[0] not in commands or len(argv) - 1 not in commands[argv[0]][1]:
        print(__doc__, file=sys.stderr)
        return 64
    commands[argv[0]][0](*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
