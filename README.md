# ase-server-flux

The ARK: Survival Evolved dedicated server for [Flux](https://runonflux.io), as
`runonflux/ase-server-flux`. The game's Linux server (Steam app 376030, ~23 GB), installed and
updated with SteamCMD on every start, under a supervisor that:

- writes `GameUserSettings.ini` and `Game.ini` from the environment and the hub's panel (session
  name, players, password, PvE, rates, difficulty, day/night, breeding, decay, damage, stacks...),
  keeping every line the server or the owner wrote that is not one of ours
- downloads Workshop mods (anonymous) and unpacks them itself, the way the game's Steam client
  does (`.z` chunked zlib, `<id>.mod` from `mod.info` and `modmeta.info`): no arkmanager, no Perl
- keeps the **RCON private** (Source RCON on 127.0.0.1, password = the admin password, made once)
- saves before every stop: `SaveWorld` over RCON, then SIGINT. ARK then hangs about a minute and
  dies with signal 11 (measured, every time); the world is on disk by then
- follows game updates and mod updates: warns players in chat over RCON, then restarts
- makes a backup of the saves on every start and every 6 hours
- gives the hub JSON status and RCON (`flux-ase`)

## Volumes

| Path | Flux mount | What |
|---|---|---|
| `/mnt/ase/data` | `g:` (synced) | `Saved/` (the install's `ShooterGame/Saved` links here: `SavedArks/` with the world, players and tribes, `Config/LinuxServer/*.ini`), `cluster/` when `ASE_CLUSTER_ID` is set, and `flux/` |
| `/mnt/ase/server` | `ml:server` (local) | the game (~22 GB) and the unpacked mods. Downloaded again on a new node |

## Ports

| Variable | Default | |
|---|---|---|
| `ASE_PORT` | 7777/udp | the game; **the next port (7778/udp) too** |
| `ASE_QUERY_PORT` | 27015/udp | the Steam query (server list) |

RCON (27020/tcp) stays inside.

## Environment

| Variable | | |
|---|---|---|
| `ASE_MAP` | | `TheIsland` (default), `TheCenter`, `Ragnarok`, `Valguero_P`, `CrystalIsles`, `LostIsland`, `Fjordur`, `ScorchedEarth_P`, `Aberration_P`, `Extinction`, `Genesis`, `Gen2`. Free to host; players need the DLC for the last five |
| `ASE_SESSION_NAME` | `SessionName` | |
| `ASE_MAX_PLAYERS` | `MaxPlayers` | |
| `ASE_PASSWORD` | `ServerPassword` | no `?` |
| `ASE_PVE` | `serverPVE` | |
| `ASE_XP`, `ASE_TAMING`, `ASE_HARVEST` | rates | |
| `ASE_DIFFICULTY` | `OverrideOfficialDifficulty` | 5 = level 150 wild dinos |
| `ASE_MODS` | `ActiveMods` | Workshop IDs or links |
| `ASE_ADMIN_PASSWORD` | `ServerAdminPassword` | unset: made once and kept |
| `ASE_BATTLEYE` | | `true` = BattlEye on (default off) |
| `ASE_CROSSPLAY` | `-crossplay -PublicIPForEpic` | Epic players; vanilla only (Epic cannot use mods) |
| `ASE_CLUSTER_ID` | `-clusterid` | transfers between servers sharing the cluster folder |
| `ASE_RESTART_TIME` | | `HH:MM`, a daily restart with warnings |

## For the hub

| Command | |
|---|---|
| `flux-ase status` | `state`, `players` (`name`, `id`), `map`, `name`, `max_players`, `mods`, `build`, `rcon`, `pending` |
| `flux-ase rcon <command...>` | `ListPlayers`, `SaveWorld`, `Broadcast <text>`, `ServerChat <text>`, `KickPlayer <id>`, `BanPlayer <id>`, `UnbanPlayer <id>`, `DestroyWildDinos`, `SetTimeOfDay HH:MM`, `GetChat`... |
| `flux-ase settings` / `set-settings '<json>'` | every setting with its source; `map` too; the admin password |
| `flux-ase mods` / `set-mods '{"mods": [...]}'` | |
| `flux-ase restart` | |

## Measured (build 21241282, ARK 361.7)

- 22.9 GB download; TheIsland up in 60-70 s cold, ~42 s warm; 4.8-6.5 GB RAM empty.
- SIGTERM, SIGINT and `DoExit` all save, then hang ~60 s and die with signal 11 (exit 139).
- **A GameUserSettings.ini written before the first start is overwritten** with ARK's defaults
  ("ARK #546145", 70 players, no admin password, so no RCON). The session name, players and
  passwords therefore go on the launch URL (ARK writes them back into the ini); the rest stays in
  the ini, where later starts keep it.
- `docker stop`: the world is saved within the first second; Docker then kills ARK in its hang.
- A Workshop mod (Awesome SpyGlass, 1404697612) downloaded, unpacked and loaded: the server
  started with it in ActiveMods.
- `ShooterGame.log` stays empty and `-log` prints nothing: readiness is the RCON answering.
- `app_update` answers "Missing configuration" now and then: `app_info_update 1` and a second
  `app_update` in the same run.

## Versions

- **1.0.0** (2026-10-11): first release.
