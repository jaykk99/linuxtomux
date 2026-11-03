# Mesh build checkpoint (branch feat/multi-device-mesh, base 646e3d8)

Jay's requirement: any mix in one mesh (2 Termux + 1 Linux, 4 Linux + 4
Termux). Servers federate; one shared device list; commands target any
device by name and relay across servers.

## Design (decided 2026-11-03)

- Every server gets a persistent unique id `srv-<hex8>`
  (DATA_DIR/server.json, 0600) and a device name (hostname or --name).
  "linux" stays as an alias for "the server monico-cmd is talking to".
- Peer link: `monico-server start --join http://<other>:8765 [--key K]
  [--name N]`. The joining server registers on the peer like a device
  (kind="server", gets a job queue), then loops: full registry sync
  (POST /bridge/peer-sync) + long-poll for jobs on the peer.
- Registry sync: each sync carries the sender's FULL known device list;
  every record is tagged with `origin` = the server that hosts the
  device directly. Receiver stores claims keyed (via_peer, origin),
  replaced wholesale per sync; a server ignores records whose origin
  is itself. Rendering prefers the direct claim (via == origin), then
  freshest. Remote online = origin reported online AND claim < 45s old.
  Dead peer -> its devices age to offline, nothing crashes; rejoin
  re-syncs and heals.
- Relay: submit() to a remote device enqueues {"type":"relay",
  target, job} into the via-peer's queue on this server. The peer's
  link loop executes it through its own submit() (recursion handles
  chains) and posts the result back under the relay job id.
- Pairing key auth on every peer call (X-Pair-Key). --key on a box
  with no key file yet is adopted as that box's pairing key (0600).
- monico-cmd: exact device_id match, then unique name match
  (duplicates -> error listing ids); "phone" = android devices only.
  MONICO_SERVER/MONICO_TOKEN env can aim monico-cmd at any server.

## Progress

- [x] Worktree/base verified (646e3d8), v1 code read, design above.
- [x] common.py: get_or_create_server_id() (636b625)
- [x] server.py: mesh registry (snapshot/known/apply_sync), relay in
      submit(), /bridge/peer-sync, PeerLink thread, serve(--join)
- [x] cli.py: --join/--key/--name flags, resolve_from_list(), env
      endpoint override (27bc6bb)
- [x] client.py: register kind="device"; homescreen: d.self link fix
- [x] tests/test_mesh.py: sync merge, relay routing, name resolution
      (9 tests; one real bug found + fixed by the v1 proof: 'phone'
      shortcut must match client devices incl. linux-platform ones,
      never servers)
- [x] tests/run_mesh_proof.sh + proof/mesh-proof-output.txt:
      14/14 PASS, 0 failures (A + B joined, T1 on A, T2 on B; both
      list 4; T1->T2 relay with env markers; A->T2; A->linux-b; kill
      B -> T2 + B offline on A, A healthy; rejoin heals)
- [x] README.md mesh section (+ layout/proof notes)
- [x] Existing 5 unit tests pass (14 total with mesh); v1 proof
      run_proof.sh green end to end (PHONE_SIDE_OK/LINUX_SIDE_OK/200)
- [x] Push attempt made 2026-11-03: `git push -u origin
      feat/multi-device-mesh` timed out at 60s (VM egress stall).
      All 5 commits are local on the branch; push still owed when
      egress returns: same command, no rebase needed (base = local
      main 646e3d8, which is origin/main + 1 local commit).
