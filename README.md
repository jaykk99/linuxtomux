# Monico Labs Bridge: Termux <-> Linux

Turn a phone into a server, and pair it with a Linux box. One bridge,
two commands, a Monico Labs homescreen, and an optional installer that
puts the biggest uncensored model each device can run right on the
device. Pure Python standard library on both ends: no pip installs, no
builds, nothing to pay for, no keys to buy.

## Install

**Linux** (the server side; do this first, it hands you the pairing key):

```sh
git clone https://github.com/jaykk99/linuxtomux.git
cd linuxtomux
./install-linux.sh
monico-server start
```

`install-linux.sh` copies the app to `~/.monico-bridge/app` and puts
`monico-server`, `monico-cmd`, and `monico-model` in `~/.local/bin`
(make sure that is on your PATH). `monico-server start` prints the
pairing key once; the Termux install below needs it, plus this
machine's address.

**Finding the Linux box's address (read this first):** every
`<linux-host>` below means the Linux machine's real IP address on
your network. Get it ON THE LINUX BOX by running:

```sh
hostname -I
```

Use the FIRST address it prints (for example `192.168.1.23`). Type
the real address with NO angle brackets: write
`http://192.168.1.23:8765`, never `http://<192.168.1.23>:8765` and
never the literal word `<linux-host>`. Same for `<pairing key>`:
paste the actual key the server printed, brackets off. Leaving the
brackets or the placeholder words in is the one mistake that breaks
the install.

Worked example (fake numbers, real shape):

```sh
MONICO_SERVER=http://192.168.1.23:8765 MONICO_TOKEN=abc123-your-real-key ./install-termux.sh
```

**Termux** (the phone side):

```sh
pkg install python git
git clone https://github.com/jaykk99/linuxtomux.git
cd linuxtomux
MONICO_SERVER=http://<linux-host>:8765 MONICO_TOKEN=<pairing key> ./install-termux.sh
monico-connect
```

That puts `monico-connect`, `monico-cmd`, and `monico-model` in
`$PREFIX/bin` and saves the server + pairing key to
`~/.monico-bridge/client.json` (0600). Once the Linux server is already
running, the shortcut is one line from the phone:

```sh
curl -fsSL http://<linux-host>:8765/install-termux.sh | MONICO_SERVER=http://<linux-host>:8765 MONICO_TOKEN=<pairing key> bash
```

(The shortcut re-downloads the app from GitHub, so the clone route
above is the one that works fully offline of GitHub.)

**The model, both sides** (optional, uncensored, biggest the device
can run):

```sh
monico-model install
```

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

## Mesh: many devices, many servers

One server was never the limit. Any mix of devices joins one mesh:
several phones on one Linux box, several Linux boxes federated
together, or everything at once.

**More phones on one server** needs nothing new: each phone runs
`monico-connect` with its own name and shows up on the homescreen,
targetable on its own:

```sh
# phone 1 and phone 2, same server
monico-connect --name pixel-8
monico-connect --name galaxy-s22
monico-cmd galaxy-s22 'termux-battery-status'
```

**More Linux boxes**: start the second server with `--join` pointing
at the first (same pairing key everywhere; pass it once with `--key`
and that box adopts it as its own):

```sh
# on linux-b, joining the server already running on linux-a
monico-server start --name linux-b --join http://<linux-a>:8765 --key <pairing key>
```

From that moment both servers carry the SAME device list: their own
devices plus each other's. A command for a device on the far server is
relayed through that server and the result comes back the same way,
so `monico-cmd <device-name> '<command>'` works from anywhere in the
mesh:

```sh
# run from linux-a, executes on the phone that is connected to linux-b
monico-cmd galaxy-s22 'uname -a'
```

Two mixes, same commands:

```sh
# 2 Termux + 1 Linux: both phones monico-connect to the one server
# 4 Linux + 4 Termux: linux-b/c/d each start with --join pointing at
# any server already in the mesh; each phone connects to its nearest
# server; every server sees all 8 devices by name
```

Targeting rules: a device id always works; a device name works when
it is unique (if two devices share a name, `monico-cmd` says so and
lists their ids); `linux` means the server you are talking to;
`phone` is a shortcut for a connected phone when there is just one
you mean. Give each device a distinct `--name` and targeting stays
one word.

If a server goes down, its devices show as offline on the others and
everything local keeps working; when it starts again with the same
`--join`, the lists sync back up on their own. A Linux box that only
needs to BE a device (no homescreen of its own) can skip the server
entirely and just run `monico-connect` against any mesh server.

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

## Dashboard (live specs, every device)

Open `http://<linux-host>:8765/dashboard` (or tap **Dashboard** on the
homescreen; same pairing key). One page, one card per device in the
mesh, Termux phones and Linux boxes alike:

- **RAM** and **storage** as bars (used / total, percent; one storage
  bar per drive, so a mounted USB drive gets its own).
- **CPU** (model, cores, live usage), **OS** and kernel, **uptime**.
- **Battery** on the phone (and on a Linux laptop): percent, charging
  or not, temperature when the phone reports it.

Every device reports about every 10 seconds (`monico-connect` does it
by itself, on its own short request beside the command channel); the
page refreshes every 5 seconds. Servers report their own specs, and
specs ride the mesh sync, so one dashboard on any server shows the
devices of every server. A device that drops off keeps its last
reading on a dimmed card, marked Offline with how long ago it
reported.

Two honest gaps, both shown on the card rather than guessed:

- **Phone battery** needs Termux:API: `pkg install termux-api` plus
  the Termux:API app (F-Droid). Without it the card says so.
- **Phone CPU usage**: Android hides `/proc/stat` from apps, so a
  phone shows its CPU model and cores but no usage percent.

Devices running an older copy of the bridge show a "no live specs
yet" note: `git pull`, re-run the installer, restart
`monico-connect` (or `monico-server start`). The same numbers are in
`/api/devices` under each device's `specs` (shape documented in
`bridge/specs.py`). `MONICO_SPECS_INTERVAL=<seconds>` changes how
often a device reports.

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

**USB storage counts.** Device info reports every storage a device
has (internal disk plus plugged-in USB drives) in `hw.storage`, and
the installer puts models on the first storage that fits this order:
`$MONICO_MODEL_DIR` if set, else a mounted USB drive with at least
1 GB free (models go under `<usb>/monico-models`), else the internal
default. The pick's disk budget counts the chosen storage's free
space, so a roomy USB drive unlocks a bigger model than a tight
internal disk would. A USB drive plugged in after the server started
is picked up automatically within about 30 s (the server re-detects
its own hardware on an interval; clients re-detect on reconnect).
Detection is read-only: an unmounted USB device is reported as
detected-but-unmounted, never mounted or formatted for you, and
`model.json` records which storage the model actually lives on.

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
bridge/server.py     Linux side: registry, mesh sync, relay, homescreen
bridge/client.py     Termux side: channel + the phone's hosted service
bridge/cli.py        command-line entry points
bridge/specs.py      live specs each device reports (RAM, storage, CPU, OS, uptime, battery)
bridge/model_installer.py + hwdetect.py   hardware tiers, installer
ui/homescreen.html   the Monico Labs homescreen
ui/dashboard.html    the live device dashboard (/dashboard)
install-linux.sh / install-termux.sh
tests/               unit tests + run_proof.sh + run_mesh_proof.sh proofs
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

`tests/run_mesh_proof.sh` proves the mesh: two servers (the second
started with `--join`) plus two simulated clients, one per server.
It shows both servers listing all 4 nodes, a command issued on one
client executing on the other across the relay, commands from a
server reaching the far client and the far server itself, the far
side ageing to offline when a server is killed (with the survivor
still working), and the mesh healing when it rejoins. Output:
`proof/mesh-proof-output.txt`. Mesh unit tests:
`python3 tests/test_mesh.py`.
