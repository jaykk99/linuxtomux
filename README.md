# Monico Labs Bridge: Termux <-> Linux

Turn a phone into a server, and pair it with a Linux box. One bridge,
two commands, a Monico Labs homescreen, and an optional installer that
puts the biggest uncensored model each device can run right on the
device. Pure Python standard library on both ends: no pip installs, no
builds, nothing to pay for, no keys to buy.

## Quickstart: 2 steps, no IP needed

**Step 1 — Linux.** Copy-paste this whole block as-is. It updates
the checkout first (a stale copy is the #1 setup failure: old versions
do not have `--public` at all and silently ignore the flag), then
installs, then starts the server with a public URL, so the phone
needs no IP address:

```sh
if [ -d ~/linuxtomux/.git ]; then cd ~/linuxtomux && git pull; else git clone https://github.com/jaykk99/linuxtomux.git ~/linuxtomux && cd ~/linuxtomux; fi
./install-linux.sh
monico-server start --public
```

`install-linux.sh` copies the app to `~/.monico-bridge/app` and puts
`monico-server`, `monico-cmd`, and `monico-model` in `~/.local/bin`.
The server prints a public dashboard URL like
`https://<name>.trycloudflare.com/dashboard` (free Cloudflare quick
tunnel, no account, no card) plus a ready-to-paste Termux block with
your real pairing key already filled in. The public URL is new on
every start, so copy the fresh block each time you restart.

**Step 2 — Termux.** Copy the block the server printed and run it
on the phone. That is the whole phone install: it puts
`monico-connect`, `monico-cmd`, and `monico-model` in `$PREFIX/bin`
and saves the server + pairing key to
`~/.monico-bridge/client.json` (0600). Then run:

```sh
monico-connect
```

The phone connects OUT to the Linux box and stays connected. From
that moment the phone is a server too: its commands, its status page,
and its hosted files are reachable through the bridge.

Not on the same WiFi as the Linux box? Use the public URL. In the
printed Termux block, replace the `http://192.168.x.x:8765` part
with the public `https://<name>.trycloudflare.com` URL from the
server's `Public dashboard:` line (drop the `/dashboard` at the end),
in both places it appears. Everything else stays the same.

**Check it worked.** Open the public dashboard URL the server
printed and enter your PIN: the phone shows up as a card with live
RAM, storage, CPU, and battery. Or from either side, run a command
on the other:

```sh
monico-cmd phone 'uname -a'     # from Linux, runs on the phone
monico-cmd linux 'uname -a'     # from the phone, runs on Linux
```

## Same WiFi (no tunnel)

If the phone and the Linux box are on the same WiFi, skip `--public`
and run `monico-server start`. The server prints a Termux block with
the box's real LAN address; copy it as-is. On a Chromebook Linux
container the LAN address usually is not reachable from the phone, so
use `--public` there.

## Troubleshooting

**`monico-server --public` prints usage and exits.**
You missed `start`. It is always `monico-server start --public`
(`start` first, flags after).

**Server starts but shows no public URL and no Termux block.**
Stale install: versions before the dashboard update do not have
`--public` at all and silently ignore the flag. Fix it with:

```sh
cd ~/linuxtomux && git pull
./install-linux.sh
monico-server start --public
```

If `git pull` complains about local changes, move the checkout aside
and clone fresh, then run the Step 1 block again:

```sh
mv ~/linuxtomux ~/linuxtomux.old
```

**`cloudflared is not installed.`**
Run the one command the server prints, then start again:

```sh
sudo curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && sudo chmod +x /usr/local/bin/cloudflared
monico-server start --public
```

**Phone can't reach the server.**
Same WiFi + the LAN block, or `--public` + the public URL. The
public URL changes every time the server restarts, so copy the fresh
block after every restart.

**`monico-server: command not found`.**
Open a new terminal so `~/.local/bin` is on your PATH, or run
`~/.local/bin/monico-server start --public`.

**No internet on the phone?** The printed command fetches the
installer AND the app files from the Linux box itself
(`/linuxtomux.tar.gz`), so it works with no internet on the phone
as long as it is on the same WiFi. The clone route below is the
fallback for a phone that is not on the same network yet (it needs
internet for the `git clone` only). Use the address and key from
your server's startup output in place of the example numbers:

```sh
pkg install python git
git clone https://github.com/jaykk99/linuxtomux.git
cd linuxtomux
MONICO_SERVER=http://192.168.1.23:8765 MONICO_TOKEN=abc123-your-real-key ./install-termux.sh
monico-connect
```

---

## The two commands

**On the Linux box:**

```sh
monico-server start --public
```

It prints the pairing key once (saved at `~/.monico-bridge/pairing-key`,
0600), prints the ready-to-paste Termux command, and starts
listening. The homescreen lives at the address the server prints.
Leave off `--public` for same-WiFi only (see above).

**On the phone (Termux):** run the block the server printed, then:

```sh
monico-connect
```

`monico-connect` is the phone's one command. Run
`./install-linux.sh` once on the Linux box to put `monico-server`,
`monico-cmd`, and `monico-model` in `~/.local/bin`. Run the printed
Termux block once on the phone for `monico-connect`, `monico-cmd`,
and `monico-model` in `$PREFIX/bin`.

**The model, both sides** (optional, uncensored, biggest the device
can run):

```sh
monico-model install
```

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
monico-connect --name phone
monico-cmd phone 'termux-battery-status'
```

**More Linux boxes**: start the second server with `--join` pointing
at the first (same pairing key everywhere; pass it once with `--key`
and that box adopts it as its own). Use the address and key from the
first server's startup output; the numbers here are an example:

```sh
# on linux-b, joining the server already running on linux-a
monico-server start --name linux-b --join http://192.168.1.23:8765 --key abc123-your-real-key
```

From that moment both servers carry the SAME device list: their own
devices plus each other's. A command for a device on the far server is
relayed through that server and the result comes back the same way,
so `monico-cmd <device-name> '<command>'` works from anywhere in the
mesh:

```sh
# run from linux-a, executes on the phone that is connected to linux-b
monico-cmd phone 'uname -a'
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

Open the homescreen address the server prints at startup and enter
the pairing key once (it is kept as a cookie after that). Big
phone-friendly tiles show:

- **Connected devices**: Linux box + every connected Termux device,
  live online/offline status, hardware summary.
- **Hosted here**: what each device hosts. The phone hosts a status
  page and a file index from `~/monico-host/` on the phone, served
  through the bridge at `/device/<id>/hosted/`; drop a file in
  `~/monico-host/` on the phone and it is hosted, owner-only.
- **Models**: the local model installed on each device (name, quant,
  size), or a plain "run monico-model install" note when there is none.

## Dashboard (live specs, every device)

Open the dashboard address the server prints at startup (or tap
**Dashboard** on the homescreen; same pairing key). One page, one card
per device in the mesh, Termux phones and Linux boxes alike:

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

## Public URL (--public)

See Quickstart: `monico-server start --public` launches a free
Cloudflare quick tunnel (no account, no card) and prints a public
`https://<name>.trycloudflare.com/dashboard` URL. The phone opens
that URL from anywhere. If `cloudflared` is not installed, the
server says so plainly and prints the one install command instead of
failing silently (see Troubleshooting).

A durable URL on your own Cloudflare account (named tunnel) is the
follow-up, not built here: it needs `cloudflared tunnel login`, a
tunnel, and a DNS record on your account.

## PIN gate

The dashboard is behind a PIN entry page, local URL and public URL
alike. Only the SHA-256 hash of the PIN lives in the code; the PIN
itself is never in the repo, never printed, and never logged. A
correct PIN sets a 12-hour session cookie. The pairing key still
opens everything, including the dashboard API (sign in with it on
the home page and the dashboard opens too).

## Shared terminal

The dashboard has a terminal pane: one command typed there runs on
every online device at the same time (this server plus every online
phone/Linux box in the mesh) and shows each device's output labeled,
like the two machines are one machine. It uses
`POST /api/exec-all`, which fans out through the existing `/api/exec`
relay path with the same destructive-command guard. Behind the same
PIN gate.

## Paired devices (two devices as one)

On the dashboard, **Pair devices** → name the pair → tick the
devices → **Pair them**. The pair gets one card:

- **Shared specs**: RAM, storage and CPU cores of the online members
  added up, plus the lowest battery in the pair.
- **Separate specs**: each member's own bars underneath, so you can
  see which one is filling up.
- **One terminal** for the pair. In **Split** mode each command goes
  to one member: they take turns (half and half on two healthy
  devices), and a member that is running out (RAM 85%+ full, or
  battery 15% or less and not charging) is spared while the other has
  room. Put several commands on separate lines and they are dealt out
  across the members and run at the same time. **Every device** mode
  runs the command on all online members at once. Each result is
  tagged with the device that ran it.

From a shell, a pair is a target like any device:

```sh
monico-cmd duo 'python3 crunch.py part1'     # the pair picks who runs it
monico-cmd duo --all 'git pull'              # every member
```

What pairing does not do: it does not merge memory. One program
still runs on one device and can only use that device's RAM, so a
job bigger than either device still will not fit; pairing decides
which device takes each job and keeps both busy. Commands are
independent shells, so `cd` and exported variables do not carry from
one command to the next (it may land on the other device): chain
with `&&` on one line. Pairs are saved on the server they were made
on (`~/.monico-bridge/pools.json`) and can include devices from any
server in the mesh. Rules live in `bridge/pools.py`.

## No internet on the phone (offline-first)

The control channel needs no internet on the phone at all: register,
specs, sync, and the shared terminal are plain HTTP from the phone
to the Linux box, no DNS and no cloud service involved.

- **Same WiFi (primary):** the phone opens the Linux box directly at
  its LAN address. The server prints the LAN dashboard URL and the
  ready-to-paste Termux command at startup. No internet needed on
  either side for the dashboard, terminal, or commands.
- **USB (fallback, no shared WiFi):** plug the phone in and run
  `adb reverse tcp:8765 tcp:8765` on the Linux box; the phone then
  reaches the server at `http://127.0.0.1:8765`. Documented, not
  live-verified here (no Android device on this box to test against).
- **Hotspot (fallback):** a WiFi hotspot from either side puts both
  on the same LAN, and the same-WiFi route above applies. Also
  documented, not live-verified here.

When the phone DOES need the internet (opening the public Cloudflare
dashboard URL, downloading anything), it goes through the Linux box:
the bridge server is also a small HTTP proxy on the same port
(CONNECT for https, plain forwarding for http). In Termux, point the
proxy at the Linux box's LAN address from the server's startup
output, with the pairing key as the password:

```sh
export http_proxy=http://monico:PAIRING_KEY_FROM_SERVER_STARTUP@LAN_ADDRESS_FROM_SERVER_STARTUP:8765
export https_proxy=$http_proxy
export no_proxy=127.0.0.1,localhost,LAN_ADDRESS_FROM_SERVER_STARTUP
```

That covers `curl`, `wget`, `pkg`, and anything else that honors
proxy env vars. The proxy needs the pairing key (Basic auth, key as
password), so a stranger on the WiFi cannot use it. A phone browser
can use it too via manual proxy settings (Firefox Android prompts
for the proxy password). The dashboard page itself is one small
self-contained file with no CDN, so it stays light through the proxy.

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
| Android phone (8 GB, Android reserve) | 4.16 GB | Dolphin3.0-Qwen2.5-3B Q8_0 | `bartowski/Dolphin3.0-Qwen2.5-3B-GGUF` |
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
bridge/tunnel.py     Cloudflare quick tunnel for --public
bridge/specs.py      live specs each device reports (RAM, storage, CPU, OS, uptime, battery)
bridge/model_installer.py + hwdetect.py   hardware tiers, installer
ui/homescreen.html   the Monico Labs homescreen
bridge/pools.py      paired devices: shared specs, how the one terminal deals commands out
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
the bridge, and prints model tier picks for an 8 GB Android phone profile, a
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
