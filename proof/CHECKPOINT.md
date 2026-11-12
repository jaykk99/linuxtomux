# Build checkpoint (updated 2026-11-03 ~17:20 EST)

## CORRECTION 17:20 EST (supersedes the 17:12 note on the GGUF)
- The GGUF download is COMPLETE and verified, not 96.2%. The depth-2
  download worker's curl exited 0 at 397,811,008 bytes; the
  413,316,096 "total" in the first worker's dl.log was a bad
  denominator from a resumed-range progress line.
- Verified three ways: (1) curl exit 0, HTTP 200; (2) SHA256
  6a53689e2cb91027fdc9e366142eba8e35f56c14ee353e0a4d64de981efbfffa,
  identical across independent reads; (3) GGUF structure parse:
  version 3, 290 tensors, 79 metadata KV, 494,032,768 params (exactly
  Qwen2.5-0.5B), computed tensor span 397,794,528 vs actual
  397,811,008 (16,480 bytes = padding/table rounding, 0.004%).
- Still blocked: llama-cli runner (GitHub egress) and the GitHub push
  (gh times out). Both are egress-only, retry in small increments.

## Update 17:12 EST
- COMMITTED: git commit 030dafa on main (all code, tests, UI, docs,
  proof log; models + tools-build gitignored).
- GGUF download reached 397,811,008 bytes, then the egress proxy
  stalled (HF + GitHub + gh timing out in that window). The stuck
  curl session was killed cleanly; the partial file was intact and
  the parallel worker's download completed it (see CORRECTION).

## Done and on disk
- Repo scaffolded at ~/workspace/termux-linux-bridge/ (git init, branch main, NOT yet committed).
- Code complete: bridge/common.py, hwdetect.py, model_installer.py,
  server.py, client.py, cli.py; launchers monico-server / monico-connect
  / monico-cmd / monico-model; install-linux.sh, install-termux.sh;
  ui/homescreen.html; integrations/termux-code/ (README + TokenPanel
  patch, patch VERIFIED applying cleanly against current TokenPanel.jsx).
- Unit tests: 5/5 PASS (python3 tests/test_bridge.py). Tier picks:
  android-8gb -> Dolphin3.0-Qwen2.5-3B Q8_0 (3.3 GB, budget 4.16 GB);
  (profile renamed from galaxy-s22, 2026-11-12; same 8 GB specs)
  linux-vm-8gb -> Dolphin3.0-Llama3.1-8B Q4_K_M; linux-box-32gb ->
  Dolphin3.0-Qwen2.5-32B Q5_K_M (22.4 GB, budget 23.2 GB).
- E2E proof PASSED, full log in proof/proof-output.txt:
  server start on :8877, pairing key, simulated Termux client registered
  (device dev-d56f1a17, name termux-sim), Linux->phone cmd PHONE_SIDE_OK
  exit 0, phone->Linux cmd LINUX_SIDE_OK exit 0, homescreen HTTP 200 with
  MONICO LABS + tiles, login page without key, phone hosted status.json
  and files/hello.txt fetched THROUGH the bridge proxy, 3 model dry runs.

## In flight (background, resumable)
- GGUF download (curl -C -, auto-resume): proof/models/
  Dolphin3.0-Qwen2.5-0.5B-Q4_K_M.gguf — was 311,304,192 /
  413,316,096 bytes (75.3%) when the earlier background curl died;
  resume started ~17:04 EST, session proc_6fb8242f760d.
  To resume manually: cd proof/models && curl -L -C - --retry 8
  --retry-all-errors -o <same filename> <HF resolve URL in
  bridge/model_installer.py CATALOG[0]>.
  Then verify: size == 413316096 and `head -c 4` == "GGUF".
- llama.cpp prebuilt release zip: nohup bash writing
  tools-build/release.log + tools-build/llama-bin.zip (resolves latest
  tag via releases/latest redirect; github.com egress has been timing
  out, may fail; git clone also failed earlier: CONNECT tunnel died).
  Fallback if zip fails: git clone --depth 1 llama.cpp + cmake build
  llama-cli -j2 (cmake/make/g++ present). If both fail, smoke test is
  blocked on egress; installer still HEAD/download-proven by GGUF.

## Finish pass 2026-11-03 ~17:55 EST (parent follow-up agent)
- Route (a) tools-build/pypi: EMPTY directory, nothing usable. No local
  llama binary/module anywhere (no llama-cli in PATH, no llama_cpp
  module, nothing under ~/workspace/tools).
- Route (b) pip install llama-cpp-python (target tools-build/pylib,
  --timeout 20 --retries 1): FAILED, pypi.org read timeout, "No matching
  distribution found" (index unreachable through the stalled proxy).
- Route (c) GitHub release zip: FAILED at the resolve step,
  releases/latest returned 000 (curl exit 28, 30s timeout). Same proxy
  stall that killed the zip twice before; asset tag not even resolvable.
- Route (d) git clone --depth 1 llama.cpp: FAILED, clone hung through
  the proxy and was killed at a 60s timeout, no source landed (same
  "Proxy CONNECT aborted" family as the earlier clone.log).
- CONCLUSION: no runner obtainable while the VM egress stall lasts.
  Smoke test stays honestly NOT RUN; model.json smoke field left in its
  current truthful state ("no llama.cpp runner on this device yet").
  The GGUF itself is downloaded, SHA256-verified and GGUF-parsed, and
  the installer run is proven; only inference is egress-blocked.
- Re-ran tests 2026-11-03 ~17:58 EST: tests/test_bridge.py 5/5 PASS;
  tests/run_proof.sh full E2E re-ran clean (exit 0, proof-output.txt
  regenerated: round trip both ways, homescreen 200 + tiles, phone
  hosted files through the bridge, 3 tier dry runs).
- REPORT.md written at repo root. Finish commit dce44cd (REPORT.md,
  this checkpoint, re-run proof log, install-home/model.json, plus a
  .gitignore line for proof/install-home/models/ so the GGUF can never
  be swept into git). Working tree clean.
- PUSH ATTEMPT 2026-11-03 ~18:00 EST: FAILED. `gh repo create
  jaykk99/termux-linux-bridge --private --source . --push` died with
  `Get "https://api.github.com/users/jaykk99": unexpected EOF`; no
  remote was set. Direct `git ls-remote` of the repo URL timed out at
  45s with no output. Nothing pushed, nothing claimed. Owed commands
  are in REPORT.md ("Push state" section).

## Remaining increments
1. Poll GGUF download; verify size + GGUF magic.
2. Check tools-build/release.log; unzip llama-bin.zip if landed, find llama-cli.
3. Real installer run: MONICO_HOME=~/workspace/termux-linux-bridge/proof/install-home
   python3 monico-model install --small (uses the downloaded GGUF path
   only if placed/linked into that home's models dir; otherwise the
   installer downloads itself, also resumable by re-run).
   Then llama-cli smoke: -n 16, capture tail + timing.
4. Re-run unit tests once more; git add + commit (exclude tools-build/,
   proof/models/ via .gitignore, already written).
5. Push attempt: gh repo create jaykk99/termux-linux-bridge or git
   remote + push; verify with git ls-remote. If egress blocks, report
   local-only commit honestly.
6. Final report to parent (commands, tier table, proof, blockers).
