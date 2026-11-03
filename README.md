# Monico Labs Bridge: Termux <-> Linux

Turn a phone into a server, and pair it with a Linux box. One bridge,
two commands, a Monico Labs homescreen, and an optional installer that
puts the biggest uncensored model each device can run right on the
device. Pure Python standard library on both ends: no pip installs, no
builds, nothing to pay for, no keys to buy.

## The two commands

**On the Linux box:**

```sh
monico-server start
```

It prints a pairing key once (saved at `~/.monico-bridge/pairing-key`,
0600) and starts listening. The homescreen lives at
`http://<linux-host>:8765/`.

**On the phone (Termux):**

```sh
pkg install python git
curl -fsSL http://<linux-host>:8765/install-termux.sh | MONICO_SERVER=http://<linux-host>:8765 MONICO_TOKEN=<pairing key> bash
monico-connect
```

`monico-connect` is the phone's one command. The phone connects OUT to
the Linux box and stays connected. From that moment the phone is a
server too: its commands, its status page, and its hosted files are
reachable through the bridge.

Run `./install-linux.sh` once on the Linux box to put `monico-server`,
`monico-cmd`, and `monico-model` in `~/.local/bin`. Run
`install-termux.sh` (or the curl line above) once on the phone for
`monico-connect`, `monico-cmd`, and `monico-model` in `$PREFIX/bin`.

## Why the phone connects out

Phones sit behind carrier NAT and change IPs constantly, so a server
can never reliably dial a phone first. The phone opens one long-poll
channel to the Linux box and holds it open; anything the Linux side
wants from the phone (a command, a page from the phone's own hosted
service) is handed down that already-open channel, and results come
back up it. No port forwarding, no public IP on the phone, no VPN
required. A plain HTTP long-poll was picked over websockets because
both ends are Python standard library only, so the identical code runs
in Termux with zero installs.

## Commands both ways

```sh
# from Linux, run something on the phone
monico-cmd phone 'termux-battery-status; uname -a'

# from the phone, run something on Linux
monico-cmd linux 'systemctl status my-service'
```

Both sides run the same `monico-cmd <target> <command...>`. Targets
are `linux`, `phone`, or a device name. A short destructive-command
guard (adapted from linux-remote) blocks fat-finger disasters like
`rm -rf /` on both ends; every command through the server is logged to
`~/.monico-bridge/bridge.log`. Whoever holds the pairing key has shell
on both devices: treat it like the linux-remote API key, because it is
the same pattern and the same stakes.

## Homescreen (Monico Labs hosting)

Open `http://<linux-host>:8765/` and enter the pairing key once (it is
kept as a cookie after that). Big phone-friendly tiles show:

- **Connected devices**: Linux box + every connected Termux device,
  live online/offline status, hardware summary.
- **Hosted here**: what each device hosts. The phone hosts a status
  page and a file index from `~/monico-host/` on the phone, served
  through the bridge at `/device/<id>/hosted/`; drop a file in
  `~/monico-host/` on the phone and it is hosted, owner-only.
- **Models**: the local model installed on each device (name, quant,
  size), or a plain "run monico-model install" note when there is none.

## Optional model installer (uncensored, on-device)

Explicit opt-in only. It never runs by itself.

```sh
monico-model install              # this device
monico-model install --dry-run    # show the pick, install nothing
monico-model install --device both
```

`--device both` installs on this device and walks the other device
through its own pick (each device is measured on its own hardware; run
it there, or trigger it from the homescreen while connected).

It measures RAM, free storage, CPU cores and architecture, GPU if any,
then picks the **biggest uncensored model the device can actually run
with headroom**: weights may use at most usable RAM / 1.25, and at most
half the free disk. Usable RAM on Android is total minus a 2.8 GB
system reserve; on Linux it is MemAvailable. Uncensored only:
Dolphin-lineage GGUF builds (bartowski builds on Hugging Face, public
and keyless). Never an RLHF-censored default, never a hosted API model.

| Device class | Measured budget | Pick | File |
|---|---|---|---|
| Small phone / 4 GB RAM | ~1 GB | Dolphin3.0-Qwen2.5-1.5B Q8_0 | `bartowski/Dolphin3.0-Qwen2.5-1.5B-GGUF` |
| Galaxy S22 (8 GB, Android reserve) | 4.16 GB | Dolphin3.0-Qwen2.5-3B Q8_0 | `bartowski/Dolphin3.0-Qwen2.5-3B-GGUF` |
| Linux VM, 8 GB (6.5 GB available) | 5.2 GB | Dolphin3.0-Llama3.1-8B Q4_K_M | `bartowski/Dolphin3.0-Llama3.1-8B-GGUF` |
| Linux box, 32 GB | 23.2 GB | Dolphin3.0-Qwen2.5-32B Q5_K_M | `bartowski/Dolphin3.0-Qwen2.5-32B-GGUF` |
| Linux box, 64 GB | 46.4 GB | Dolphin3.0-Qwen2.5-72B Q4_K_M | `bartowski/Dolphin3.0-Qwen2.5-72B-GGUF` |

The full catalogue (0.5B to 72B, Q4_K_M / Q5_K_M / Q8_0) is in
`bridge/model_installer.py`. The installer HEAD-checks the exact GGUF
file before downloading, saves the pick to
`~/.monico-bridge/model.json` (which is what the homescreen model tile
reads), and, when a llama.cpp runner (`llama-cli`) is on the device,
smoke-tests a short completion and records the result. To add the
runner on Linux: build llama.cpp once
(`git clone https://github.com/ggerganov/llama.cpp`, cmake build of
`llama-cli`); on Termux: `pkg install llama-cpp` where available, or
the same source build. If no runner exists yet, the model file still
installs and the config honestly says so.

## termux-code integration

The termux-code gate hands out one copy-paste Termux command with a
per-owner token. The bridge uses the same hand-off and can accept the
same token: see `integrations/termux-code/` for the write-up and a
(TokenPanel) patch that adds a second copy block for
`monico-connect`. The patch is built and verified; deploying it to the
live gate is a separate step.

## Layout

```
monico-server / monico-connect / monico-cmd / monico-model   commands
bridge/server.py     Linux side: registry, command channel, homescreen
bridge/client.py     Termux side: channel + the phone's hosted service
bridge/cli.py        command-line entry points
bridge/model_installer.py + hwdetect.py   hardware tiers, installer
ui/homescreen.html   the Monico Labs homescreen
install-linux.sh / install-termux.sh
tests/               unit tests + run_proof.sh end-to-end proof
integrations/termux-code/
```

## Proof on this machine

`tests/run_proof.sh` starts the server, connects a simulated Termux
client (a clean shell standing in for the phone; real Android Termux
cannot run inside this VM), runs a command both ways, fetches the
homescreen, pulls the phone's hosted status + a hosted file through
the bridge, and prints model tier picks for a Galaxy S22 profile, a
32 GB Linux profile, and this machine. Output: `proof/proof-output.txt`.
Unit tests: `python3 tests/test_bridge.py`.
