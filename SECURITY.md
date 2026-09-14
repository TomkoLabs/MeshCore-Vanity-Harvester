# Security policy

## Private-key handling

This program generates real MeshCore identity keys. Possession of a saved private key permits use of that identity.

- Never commit, publish, paste, or attach the runtime `data/` directory.
- Treat `leaderboards_private.json`, `private_keys_by_public_key.json`, their backups, migration/merge archives (`before_score_*.private.json`, `before_merge_*.private.json`), and `leaderboard_history.private.jsonl` as secrets.
- Keep the data directory on an encrypted filesystem when practical.
- Back up private state only to encrypted storage with access controls.
- Run the harvester under a dedicated, non-privileged account on shared systems.
- Review permissions after copying files; the program enforces `0700` for the directory and `0600` for private files when it checkpoints.
- Merges accept current/legacy private files, seed lists and journals; transfer these only over an encrypted channel or encrypted removable storage. Keep staging copies at mode `0600` and remove them after verifying the merge.
- Public keys and the six-character repeater IDs are safe to share. Seeds and expanded private keys are not.

The SHA-256 integrity fields detect accidental corruption; they are not signatures and do not protect against a malicious party who can modify the files.

## Independent harvested identities

Prefix harvesting draws independent OS-random starting scalars for each short
CPU/GPU chain and retains at most one identity from each chain. A hit retires
that chain; subsequent chains are independently reseeded. Do not modify it to
retain several nearby `scalar + 8*i` results from the same chain: publishing
or exposing one private scalar could then expose related harvested identities.
The independent random signing-prefix bytes do not remove that relationship.

GPU output buffers are bounded. Overflow replays the same independent starts
in smaller disjoint groups, without publishing the incomplete original batch.
Every emitted scalar/public-key pair is verified in Rust and every accepted
record is independently re-derived in Python before persistence. Native error
messages must never include private scalars. `harvest --benchmark` prints only
statistics and discards key material.

## Reporting a vulnerability

Do not include generated private keys, seeds, or private state files in a report. Use a private security-reporting channel configured for the repository. If none exists, open a minimal issue requesting private contact without disclosing exploit details or secrets.
