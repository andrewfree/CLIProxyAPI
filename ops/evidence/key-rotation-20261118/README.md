# Client and provider key rotation, November 18, 2026

These scripts were used for the "Client key rotation" and "Provider keys" items in
`ops/FLEET.md`, "Operations on November 18, 2026". That section records the following:
- Two new CLIProxy client keys were added at the top of both hosts' key lists, and every client
  moved to them. Review agents had printed the old keys in their own transcripts.
- A new Z.ai Coding Plan key replaced the old one on both hosts and in CodexBar.
- A new NVIDIA key replaced the old one in the Mac config and `~/.config/nvidia/api.env`.
- Both old provider keys were deleted at the providers. None of these scripts calls a provider.

These are as-run records, not maintained tools. Their paths and hosts are those of 2026-11-18.
They are byte-identical copies of the private files in `ops/runtime/key-rotation-20261118/` on the
Mac. No key, key hash, key file, revoke spec, backup or receipt is in this directory. Check a copy
with `shasum -a 256 <file>`.

## Order

1. `add_keys.py` added the two new client keys to each host's config.
2. `rotate_t3_keys.mjs` and `replace_secret.py` moved the clients and the provider keys to the new
   values.
3. `check_keys.py`, `scan_secrets.py` and `scan_hashes.py` verified the result.
4. The old client keys stay accepted until a scheduled task removes them on 2026-11-19 at
   10:30 UTC. That task runs the private runtime copy of `remove_keys.py` with a revoke spec that
   holds only key hashes. It had not run when this record was committed. The copy here holds the
   bytes of the runtime copy on November 18.

`keys_json.sh` and `zai_pair.sh` build the stdin for the other scripts. Their output contains key
values, so it was only piped, never shown.

## Scripts

- `add_keys.py` (sha256 `1afd11d8e5b58d00e98cda57443a97c7aadb01c419bbcc7e456e262a7ed6a9c5`): reads stdin `{"new": [k1, k2], "old_sha256": [h1, h2]}` and takes the config path and a backup directory as arguments. It refuses unless both old keys are present (matched by sha256) and neither new key is. It inserts the new keys as list lines just before the first old key, with the same indent and quoting, writes a 0600 backup and rewrites the file in place, keeping the inode. It prints one JSON line: lines inserted, line number, whether the inode is unchanged, mode, 12-character sha256 prefixes of the file before and after, and the time.
- `rotate_t3_keys.mjs` (sha256 `9808235ec2e0f317bedc4cfbe219c9613c74ea00c1b75581e3743f4662d124a5`): run as `node rotate_t3_keys.mjs <t3-base-dir> <configure_claude_pool.mjs> <backup-dir> <key-file> <key-index> <id>...`. It connects to T3 with `connectT3` from `ops/configure_claude_pool.mjs`. For each named `claudeAgent` provider instance, it sets the sensitive `ANTHROPIC_API_KEY` to entry `key-index` of the key file's `new` list. It applies the change through `server.updateSettings` and saves the 0600 before and after settings to the backup directory. It refuses an instance that is not `claudeAgent` or has no sensitive `ANTHROPIC_API_KEY`. It prints the updated IDs and, for each instance, only whether its key entry is sensitive, redacted and visible.
- `replace_secret.py` (sha256 `971dc3e513a153b4c6585db8ebd056b84e9212951176efbd43cdefeb8098f7ea`): reads stdin `{"old": "...", "new": "..."}` and takes a file and a backup directory as arguments (`~` is expanded). It refuses unless the old value occurs exactly once and the new value is absent. It writes a 0600 backup and replaces the value in place, keeping the inode. It prints the file argument, the replacement count, inode and mode, and 12-character sha256 prefixes of the file before and after.
- `keys_json.sh` (sha256 `385899c6c9bdf54a8a10155d59ddbdabc6695f6a8a029fe9b600cd9df4a4b30c`): Mac only. It reads `~/cliproxy/config.yaml` and the private `new-client-keys.json` beside the script through `uv run` with PyYAML. It prints `{"new": [...], "old": [...]}`, the client key values, which is the stdin that `check_keys.py` and `scan_secrets.py` read.
- `zai_pair.sh` (sha256 `be4cd8d027fb990763fd63125ad0fe71d3e947d8109486a40d0ca401bd408a49`): Mac only. It prints `{"old": ..., "new": ...}`: the current `zai-coding-plan` key from `~/cliproxy/config.yaml` and the private `zai-new.key` beside the script. That is the stdin `replace_secret.py` reads.
- `check_keys.py` (sha256 `a771cb4b6a500481e08d9007b3e4f01046176b8c8f2fcda9843dde5f3b7cc67b`): reads stdin `{"new": [...], "old": [...]}` and takes a host base URL as an argument. It sends `GET /v1/models` with each key as a bearer token. It prints one JSON object that maps the labels `new1`, `new2`, `old1` and so on to an HTTP status code or an error type name.
- `scan_secrets.py` (sha256 `38b619f865d4729c9dc87f048bd5047d7a70933505da1d0b1b61e16d305eb5d7`): reads stdin `{"new": [...], "old": [...]}` and takes the T3 secrets directory as an argument. It checks each `provider-env-*` file whose name contains the base64 form of `ANTHROPIC_API_KEY`. For each one it prints the decoded provider instance name and the key labels the file contains, or `no known key (possibly encrypted)`.
- `scan_hashes.py` (sha256 `af52c7ecdca1179d276041e27d89d28dbec445f22d19f098916ef18f183685cb`): reads stdin, a JSON object that maps key sha256 values to labels. It walks a fixed list of user config locations: `~/.config`, `~/.local/bin`, the OpenCode and Grok directories, the T3 secrets, settings and service env, the Codex and Claude settings, `~/.zshenv`, `~/.zshrc`, and the CLIProxy config and `.env`. It splits each file into tokens. For every file that holds a token with a listed hash, it prints the short host name, the path relative to `~`, and the label. It needs only hashes, not key values.
- `remove_keys.py` (sha256 `d33b9462a7efa12878422ce3700a0d14a64fec605cb11573d9fcd96ec262c429`): reads stdin `{"old_sha256": [...], "keep_sha256": [...]}` and takes the config path, a backup directory and an optional `--dry-run` as arguments. It refuses unless every old hash matches exactly one list line and every keep hash is present. Otherwise it removes the old key lines, writes a 0600 backup and rewrites the file in place, keeping the inode. A dry run prints `would_remove` and `keep_present`. A real run prints `removed`, `keep_present`, whether the inode is unchanged, and a 12-character sha256 prefix of the result.
