# Runtime data

This directory is intentionally excluded from Git except for this file.

It contains generated public leaderboards, private MeshCore keys, backups, GPU progress, a process lock, and an append-only recovery journal. Do not commit, publish, or attach its contents. The harvester changes this directory to mode `0700` and writes private files as `0600`.
