# Security policy

## Private-key handling

This program generates real MeshCore identity keys. Possession of a saved private key permits use of that identity.

- Never commit, publish, paste, or attach the runtime `data/` directory.
- Treat `leaderboards_private.json`, `private_keys_by_public_key.json`, their backups, and `leaderboard_history.private.jsonl` as secrets.
- Keep the data directory on an encrypted filesystem when practical.
- Back up private state only to encrypted storage with access controls.
- Run the harvester under a dedicated, non-privileged account on shared systems.
- Review permissions after copying files; the program enforces `0700` for the directory and `0600` for private files when it checkpoints.
- Public keys and the six-character repeater IDs are safe to share. Seeds and expanded private keys are not.

The SHA-256 integrity fields detect accidental corruption; they are not signatures and do not protect against a malicious party who can modify the files.

## Reporting a vulnerability

Do not include generated private keys, seeds, or private state files in a report. Use a private security-reporting channel configured for the repository. If none exists, open a minimal issue requesting private contact without disclosing exploit details or secrets.
