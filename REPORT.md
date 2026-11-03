# Monico Labs Termux <-> Linux Bridge: final report

Built and proven locally on this VM (2026-11-03). Real Android Termux
cannot run inside this VM, so the phone side was proven with a clean
shell standing in for it; the real-phone test is still Jay's (below).

## The two commands

**Linux side (start command):**

```sh
./install-linux.sh        # once: puts the launchers in ~/.local/bin
monico-server start
```

Prints the pairing key once (saved 0600 at
`~/.monico-bridge/pairing-key`) and serves the bridge + homescreen.

**Termux side (connect command):**

```sh
pkg install python git
curl -fsSL http://<linux-host>:8765/install-termux.sh | MONICO_SERVER=http://<linux-host>:8765 MONICO_TOKEN=<pairing key> bash
monico-connect
```

The phone connects OUT to the Linux box (phones sit behind carrier
NAT, so the server can never dial the phone first) over an HTTP
long-poll channel, Python standard library only, zero installs. Once
connected, the phone is itself a server through the bridge: its status
page, its hosted files (`~/monico-host/`), and remote commands on it
are all reachable through the Linux side, owner-only via the pairing
key.

**Commands both ways:** `monico-cmd phone '<command>'` from Linux,
`monico-cmd linux '<command>'` from the phone. A destructive-command
guard runs on both ends; every command is logged.

## Proof: commands both ways (real output, proof/proof-output.txt)

- Linux -> phone: output `PHONE_SIDE_OK` + `Linux` + `root`, exit 0.
- Phone -> Linux: output `LINUX_SIDE_OK` + `htch-runtime`, exit 0.
- Phone hosted service fetched THROUGH the bridge proxy: device
  status JSON plus `files/hello.txt` returned
  `hosted-on-the-phone proof file`.
- E2E proof re-ran clean on 2026-11-03 (exit 0) during the finish pass.

## Homescreen

- As tested locally: `http://localhost:8877/` during the proof run
  (server started on port 8877 by tests/run_proof.sh). The deployed
  default in the README is `http://<linux-host>:8765/`.
- With the pairing key: HTTP 200, MONICO LABS homescreen with device
  tiles (12 tile markers counted in the proof), showing connected
  devices with live status, what each device hosts, and per-device
  model status. Without the key: owner login page only.

## Model installer (optional, opt-in, uncensored only)

```sh
monico-model install              # this device
monico-model install --dry-run    # show the pick, install nothing
monico-model install --device both
```

It measures RAM, free storage, CPU cores/arch and GPU, then picks the
biggest uncensored GGUF the device can run with headroom (weights at
most usable RAM / 1.25 and at most half the free disk; Android usable
RAM = total minus a 2.8 GB system reserve). Dolphin lineage only,
bartowski builds on Hugging Face, public and keyless. Never an
RLHF-censored default, never GPT/Claude/Gemini, never a hosted model.

Tier table (device class -> pick):

| Device class | Measured budget | Pick | Repo |
|---|---|---|---|
| Small phone / 4 GB RAM | ~1 GB | Dolphin3.0-Qwen2.5-1.5B Q8_0 (1.70 GB file) | bartowski/Dolphin3.0-Qwen2.5-1.5B-GGUF |
| Galaxy S22 (8 GB, Android reserve) | 4.16 GB | Dolphin3.0-Qwen2.5-3B Q8_0 (3.30 GB file) | bartowski/Dolphin3.0-Qwen2.5-3B-GGUF |
| Linux VM, 8 GB (6.5 GB available) | 5.2 GB | Dolphin3.0-Llama3.1-8B Q4_K_M (4.95 GB file) | bartowski/Dolphin3.0-Llama3.1-8B-GGUF |
| Linux box, 32 GB | 23.2 GB | Dolphin3.0-Qwen2.5-32B Q5_K_M (22.4 GB file) | bartowski/Dolphin3.0-Qwen2.5-32B-GGUF |
| Linux box, 64 GB | 46.4 GB | Dolphin3.0-Qwen2.5-72B Q4_K_M (43.5 GB file) | bartowski/Dolphin3.0-Qwen2.5-72B-GGUF |

Full catalogue in `bridge/model_installer.py` (CATALOG): 0.5B, 1.5B,
3B, 7B, 8B (Llama3.1), 14B, 32B, 72B, each in Q4_K_M plus Q5_K_M and/or
Q8_0 where published, all Dolphin (uncensored) lineage. Dry-run tier
picks proven in the E2E log for a Galaxy S22 profile, a 32 GB Linux
profile, and this VM, and the S22 vs big-Linux picks land on different
tiers (unit-tested).

**Real installer run (done):** the small smoke model
`Dolphin3.0-Qwen2.5-0.5B-Q4_K_M.gguf` (397,811,008 bytes) was
downloaded in full and verified three ways: curl exit 0 / HTTP 200,
SHA256 `6a53689e2cb91027fdc9e366142eba8e35f56c14ee353e0a4d64de981efbfffa`
identical across independent reads, and a GGUF structure parse
(version 3, 290 tensors, 494,032,768 params = exactly Qwen2.5-0.5B).
A real `monico-model install --small` run landed it in
`proof/install-home/`; the resulting `model.json` is committed as
`proof/install-home/model.json`.

## Smoke test: NOT RUN, honestly blocked

No inference smoke result exists and none is claimed. The model file
is installed and verified, but there is no llama.cpp runner on this
VM, and every route to one is blocked by the VM egress stall
(all attempts 2026-11-03, proof/CHECKPOINT.md has the log):

1. tools-build/pypi: empty, nothing usable pre-staged.
2. `pip install llama-cpp-python`: pypi.org read timeout through the
   stalled proxy, no distribution fetched.
3. GitHub prebuilt release zip: releases/latest probe returned 000
   (curl timeout); earlier attempts died with "Proxy CONNECT aborted".
4. `git clone --depth 1 llama.cpp` + cmake build: clone hung through
   the proxy, killed at timeout; earlier attempt failed the same way.

`proof/install-home/model.json` keeps its truthful smoke field:
`{"ok": false, "error": "no llama.cpp runner on this device yet ..."}`.
To finish the smoke once egress is back: get `llama-cli` (release zip
or the clone + cmake build of llama-cli), then run it against
`proof/install-home/models/Dolphin3.0-Qwen2.5-0.5B-Q4_K_M.gguf` with a
short prompt and `-n 16`, and record the output in model.json.

## Tests

- Unit: `python3 tests/test_bridge.py` — 5/5 PASS (re-run
  2026-11-03 ~17:58 EST): denylist, catalogue URLs all HF-resolve,
  catalogue uncensored-only, S22 vs big-Linux different tiers, VM
  class picks 7B/8B Q4.
- E2E: `tests/run_proof.sh` — full pass, exit 0, re-run in the finish
  pass; log in `proof/proof-output.txt`.

## Git state

Commits on `main`:

- `030dafa` Monico Labs bridge v1: Termux <-> Linux bridge,
  homescreen, uncensored model installer
- `1b238f9` Checkpoint: egress stall state, GGUF at 96.2%, push blocked
- `2e83300` Correction: GGUF download verified complete; installer run ok
- Finish commit (this report, updated checkpoint, re-run proof log,
  install-home/model.json): the commit that adds this file; see
  `git log --oneline -1`.

## Push state: NOT PUSHED (egress-blocked)

No GitHub repo exists yet and nothing is claimed. `gh` and git
network calls time out through the same stalled proxy. Owed commands,
to run when egress returns:

```sh
cd ~/workspace/termux-linux-bridge
gh repo create jaykk99/termux-linux-bridge --private --source . --push
# or, if the repo already exists by then:
git remote add origin https://github.com/jaykk99/termux-linux-bridge.git
git push -u origin main
git ls-remote origin   # verify before claiming the push landed
```

## Still needs Jay

1. **Real-phone test:** run the Termux connect command above on the
   actual phone (Galaxy S22) against a running `monico-server`, and
   confirm the phone shows up on the homescreen and serves its hosted
   files. Everything else was proven with a simulated Termux shell.
2. **termux-code gate deploy (his tap only):** the integration in
   `integrations/termux-code/` (write-up + TokenPanel patch adding a
   `monico-connect` copy block with the same per-owner token) is built
   and the patch is verified applying cleanly, but it is NOT deployed
   to the live termux-code gate. Deploying it is Jay's call.
3. Nothing else: no keys, no accounts, no card. The bridge is
   keyless, owner-only via the pairing key it generates itself.
