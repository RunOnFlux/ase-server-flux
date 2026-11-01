# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Docker image repo**: the ARK: Survival Evolved dedicated server for Flux,
`runonflux/ase-server-flux`. steamcmd/steamcmd:ubuntu-24, the game's Linux server, and a
supervisor in bash. Same family as `~/work/theisle-server-flux`.

## Commands

```bash
./tests/test-flux.sh
docker build -t ase-server-flux:local .
./tests/test-image.sh
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh
```

## Things that will bite you

- **ARK hangs ~60 s after its shutdown save, then segfaults.** Save over RCON first, signal, and
  kill after FLUX_STOP_GRACE (20 s). Exit 137/139 after a requested stop is normal.
- **Never publish RCON**; the hub uses it from inside.
- **The ini files are shared with the server.** `set_ini` changes only our keys (case-insensitive)
  and keeps every other line.
- **The launch URL holds only the map and the ports.** Everything else is in the ini, so a `?`
  in a name or password would break nothing; passwords with `?` are refused anyway.
- **Mods: WindowsNoEditor, on Linux too** (arkmanager's default; "Linux mods are known to crash").
  `install_mod` is arkmanager's doExtractMod in Python (with its multi-map offset bug fixed).
- **No log to read**: readiness = RCON answering. ARK writes ShooterGame.log only on exit.
- **`AltSaveDirectoryName` is a URL option** (`?AltSaveDirectoryName=SavedArks/<Map>`); as a dash
  argument it is ignored (measured). Each map's folder holds its own players and tribes.
- **Dated `.ark` copies are never deleted by ARK**: `prune_dated` keeps FLUX_DATED_KEEP.
- **A restore cannot be done by an exec**: `flux-ase restore` records it and restarts; the
  supervisor applies it before the next start (`restore-apply`), after a `pre-restore` backup.
- **`flux.log` is local** (`$FLUX_SERVER_DIR/flux-logs/flux.log`, the `ml:` install volume; 5 MB then one `.1`), never on the synced data volume: a constantly written file there is constant Syncthing traffic. `flux_log_migrate` moves an old one off `data/flux/` at start. `flux_banner` prints the FluxCloud banner at container start and every generation; its text is tested. `state.json` and the backups stay in `data/flux/`.
- **The scheduled CI run publishes only with something to apply** (`.github/ci/needs-rebuild.sh`): urgent security updates, or commits on main since the published `:latest` (label `org.opencontainers.image.revision`) that touch the image inputs; anything else waits for the first Monday of the month.
- **`scripts/catalog.json` is the settings list, and the hub has a copy** (`npm run ase:sync-catalog` in games-website; its test fails when they differ). `flux-ase` is a SYMLINK to `fluxase.py`, so the catalog is found with `realpath`: with plain `__file__` the image knew 4 settings and said nothing (1.1.0 nearly shipped like that; `tests/image/in-container.sh` now checks it). A key's file and section matter: one in the wrong place silently does nothing.
- **Launch flags live in `settings.json` (`launch`, `extraArgs`), never in env**, so they need no spec change and are not locked. `apply` prints them as `ARG` lines; `OWNED_FLAGS` (port, BattlEye, mods, cluster...) are refused. A flag the game does not know is ignored without an error, hence the fixed `LAUNCH` table.
- **ini section and key names are case-insensitive** (`set_ini`, `ini_get`): people write `[/Script/ShooterGame.ShooterGameMode]` and ours is `[/script/shootergame.shootergamemode]`. Repeated-key Game.ini arrays (engrams, spawn weights, XP ramp) are NOT supported by `set_ini`, which keeps one line per key.
