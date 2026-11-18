# Fleet design prechecks, November 18, 2026

`ops/FLEET.md`, "Operations on November 18, 2026", item "Fleet design prechecks 0(a) and 0(f)",
records the result of a Kimi credential check. Will's extra `kimi-ai` credential belongs to the
same Kimi user as the `kimi` credential on both hosts (equal hashed user ID), but it signs in
through `kimi.ai` instead of `kimi.com`. `kimi_ident.py` prints hashed identity fields of the kind
that comparison needs. This record does not show its exact invocation, the hosts it ran on, or its
output. The other prechecks in that section have no script here.

This is an as-run record, not a maintained tool. Its paths and hosts are those of 2026-11-18. It
is a byte-identical copy of the private file in `ops/runtime/fleet-design-20261117/` on the Mac.
Check it with `shasum -a 256 kimi_ident.py`.

- `kimi_ident.py` (sha256 `44217967fbf41cac67b8e05d94c5bdeb54b2cd4ec03825424adfc37b0f7c8ebf`): run as `kimi_ident.py <auths-dir>`. It reads each JSON file with `kimi` in its name and prints one line per file. Each line holds a 10-character sha256 prefix of the file name, the `type`, `prefix` and `disabled` fields, and up to 12 field names that contain neither `token` nor `key`. It also holds 10-character sha256 prefixes of the identity fields (`email`, `user_id`, `account_id`, `sub`, `id`, `label`, `phone`). When the access, ID or refresh token is a JWT, the line also holds prefixes of its `sub`, `user_id`, `uid`, `email` and `account_id` claims. It prints no token or identity value. Its output is not part of this record. The hashes are unsalted, so a guessed address could be checked against them.
