# Affinity did not demonstrate a reproducible cache benefit

The operating decision is to restore round robin on the Mac fallback and Will primary, retaining routing drift checks. This follows the criterion frozen before the comparison. It is a conservative policy choice for Will, where the comparison was not run.

The experiment used the existing fleet.3 image, HTTP on both legs, Luna at low reasoning effort, two interleaved sessions per run, and about 98,000 initial input tokens from public repository source. Each session completed five function calls with linked tool results and one final exact-output check. Five pairs alternated which setting ran first. Every arm and session used a fresh early nonce and stable session, thread, and cache identifiers. First turns were excluded from the primary cache metric.

All 120 requests completed with valid token usage and checked outputs. Logs correlated every request with one selected account. Round-robin sessions each visited two accounts. Affinity sessions each stayed on one. Paired input totals differed by less than 1%.

| Pair | Round-robin uncached tokens | Affinity uncached tokens |
| --- | ---: | ---: |
| 1 | 214,766 | 15,174 |
| 2 | 15,194 | 15,194 |
| 3 | 15,244 | 15,274 |
| 4 | 15,224 | 211,290 |
| 5 | 15,224 | 213,604 |

These are totals over ten non-first requests per arm. The high totals each contain two full cache misses, with zero cached tokens. Full misses occurred in 1 of 40 requests after an account change and 5 of 60 without one. Four of the six misses occurred with affinity enabled. Account switching does not explain most of these misses in this experiment. The median paired reduction was negative 30 tokens, or negative 0.20%. One of five pairs improved. The frozen cache criterion required at least 10% median reduction, at least 10,000 fewer tokens, and improvement in every pair. It failed. This criterion targets a large, consistent improvement. It cannot establish that small or rare benefits are absent.

Latency also failed its repeatability criterion. Three pairs improved and two worsened. The median paired reduction in total request time was 12.08%, with a range from negative 131.31% to positive 37.96%. Host contention and remote cache variability limit interpretation. This is not a speedup claim. No subscription-quota or monetary savings were measured.

Ten of ten first visits to another account within a session reported cached input. So did 39 of 40 requests after an account change. These observations do not establish the upstream cache's isolation boundary or prove that affinity never helps. A separate bounded request-log sample used to choose context size overlaps the evaluation and is not an independent production baseline. Astra, Sol, Claude, WebSockets, and native T3 app-server behavior were not compared.

An earlier sweep was stopped after its first arm because round robin did not rotate the two sessions. Fixed session order aliased the two eligible accounts after a pre-existing model cooldown reduced the pool. That run remains preserved and is excluded from the comparison. The revised protocol alternated session order and added a schedule regression test before freezing a new probe hash. No failed comparison was silently discarded or replaced.

The [frozen protocol](evidence/cache-affinity-20261102/protocol.json) records the exact measured probe hash and acceptance criteria. The [sanitized analysis](evidence/cache-affinity-20261102/analysis.json) retains all paired metrics. Full private receipts and the measured probe copy are under `/Users/rever/.codex/reports/cliproxy-affinity-validation-20261102/`. The current probe additionally supports legacy and v8 configuration layouts, rejects malformed events and output, records interrupted streams, and redacts configuration parser errors. It requires an explicit valid port. Output files are reserved exclusively, and the manifest path must differ from the output and partial-output paths. These post-measurement checks do not change the recorded experiment.

## Reuse the probe deliberately

`ops/cache_probe.py` sends no request without `--execute`. Its public-source allowlist excludes credentials and private documents. Each run writes sanitized metrics and optionally a separate mode-0600 file of generated session IDs for log correlation. The loader prefers `server.port` and `access.api-keys`, with fallback to their legacy top-level spellings. Missing or invalid ports fail safely instead of guessing a listener. Raw model output stays in memory in this client; the existing proxy request logger still records normal requests.

Run the offline tests and no-call command before an authorized experiment:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v ops.test_cache_probe
python3 ops/cache_probe.py --run-id example --model gpt-6-luna --output ops/runtime/cache-probe/example.json --sessions 2 --turns 5 --context-chars 380000
```

The command prints `No requests sent. Add --execute to enable the probe.` A live run consumes provider quota and needs a fresh output path. The 180-second client socket timeout does not alter the proxy's timeout policy and is not a total run deadline. The script does not toggle affinity or choose accounts. Confirm routing from request logs before interpreting a comparison.

The final output check asks the model to repeat a supplied checksum. It checks continuation completion and exact output, not independent code comprehension or checksum computation.

## Live rollback verification

Both hosts loaded `session-affinity: false` without a container restart. The fleet verifiers and Will owner verifier passed. The rollback preserved unrelated configuration and the mounted file identity. The helper's first convergence check treated an omitted false JSON field as a mismatch. A read-only check through the existing verifier confirmed the actual state; the configuration write was not repeated.

The final Mac functional smoke completed six requests. Its first request encountered upstream `429 usage_limit_reached`, then succeeded on another account within the configured Codex pool. Subsequent requests all used that successful account. This confirms successful request handling and ordinary pooled failover, but does not prove multi-account rotation after rollback. The earlier comparison did prove rotation in its off arms. The smoke is excluded from all performance figures and was not repeated. One account was already cooling down before the comparison, so the later limit may have left only one usable Luna account. That is a capacity limitation, not evidence that the comparison caused the limit. The comparison used 12,039,260 input tokens, of which 2,713,308 were uncached.

The Gmail Claude generation recheck remains blocked by the previously observed upstream limit. Its existing one-shot task remains scheduled for November 3, 2026 at 18:05 Pacific. The check must keep the named Gmail route. No account substitution is authorized for that check.

Round robin is the earlier known operating policy, not a proven cache-safe choice for every provider. The named Gmail and iCloud Claude routes restrict account selection independently of affinity. Unprefixed Claude pooling was not evaluated for cache impact or current client usage.

## Review and reproducibility

Two GPT-6 Luna audits checked the saved comparison and the probe. Claude Opus 5.5 reviewed the complete policy and implementation and returned QUALIFIED. GPT-6 Astra applied the requested miss-pattern explanation, configuration-layout support, and source provenance before the final commit. It also fixed truncated-stream handling, malformed tool/message validation, manifest-directory creation, and the omitted-false routing regression case. These small corrections were validated offline and were not sent through a second Opus review.

Commit `777557c8` preserves the exact measured probe and its 13 tests. Verify the frozen source with:

```sh
git show 777557c8:ops/cache_probe.py | shasum -a 256
```

Measured probe SHA-256 is `ed327f1de503692cfb5a19a0bbc7494b26c3ee8adfcade6e9709354de83f93f1`. Final probe SHA-256 is `618e5798884e7353b3a16eb0cc7276364023a63ee8615b92d2a12b57bcd8bb89`. The original comparison is not attributed to the later hardened source.

Final validation passed 84 ops tests, including 21 probe tests and an added routing regression test. Tests changed to add coverage. Focused Go auth, session, service, and configuration tests passed, and the server built. The initial system-Python ops run lacked PyYAML; the complete passing run used the isolated report venv with PyYAML 6.0.3. No test was weakened to accommodate that dependency failure. No full local-ci or new native T3 acceptance result is claimed.
