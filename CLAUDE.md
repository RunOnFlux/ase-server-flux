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
- **No log to read**: readiness = RCON answering.
