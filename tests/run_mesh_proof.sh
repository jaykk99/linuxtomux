#!/usr/bin/env bash
# Multi-node mesh proof on this VM:
#   server A (linux-a) + server B (linux-b, --join A)
#   simulated Termux client T1 on A, T2 on B
# Shows: (a) both servers list all 4 nodes, (b) a command issued on
# T1 executes on T2 (cross-server relay), (c) commands from A execute
# on T2 and on B itself, (d) killing B marks T2 + B offline on A while
# A keeps working, (e) restarting B heals the mesh.
# Simulated clients are clean shells standing in for real Termux,
# the same pattern as run_proof.sh. Output: proof/mesh-proof-output.txt
set -u
cd "$(dirname "$0")/.."
REPO="$PWD"
export PYTHONPATH="$REPO"
PA=8911; PB=8912
A_HOME=/tmp/mesh-a
B_HOME=/tmp/mesh-b
T1_HOME=/tmp/mesh-t1home
T2_HOME=/tmp/mesh-t2home
OUT="$REPO/proof/mesh-proof-output.txt"
mkdir -p "$REPO/proof"
exec > >(tee "$OUT") 2>&1
FAILURES=0

rm -rf "$A_HOME" "$B_HOME" "$T1_HOME" "$T2_HOME"
mkdir -p "$A_HOME" "$B_HOME" "$T1_HOME/monico-host" "$T2_HOME/monico-host"

cleanup() { kill ${A_PID:-} ${B_PID:-} ${T1_PID:-} ${T2_PID:-} 2>/dev/null; }
trap cleanup EXIT

devs() { curl -s -H "X-Pair-Key: $KEY" "http://127.0.0.1:$1/api/devices"; }
state_of() { # port name -> "online" / "offline" / "absent"
  devs "$1" | python3 -c "
import json, sys
n = sys.argv[1]
for d in json.load(sys.stdin)['devices']:
    if d['name'] == n:
        print('online' if d['online'] else 'offline'); break
else:
    print('absent')" "$2"
}
wait_until() { # port name want seconds
  local want="$3" tries="$4"
  for _ in $(seq 1 "$tries"); do
    [ "$(state_of "$1" "$2")" = "$want" ] && return 0
    sleep 1
  done
  return 1
}
show_devices() {
  devs "$1" | python3 -c "
import json, sys
for d in json.load(sys.stdin)['devices']:
    print(f\"  {d['name']:<12} {d['platform']:<15} online={str(d['online']):<5} via={d.get('via') or '-':<8} id={d['device_id']}\")"
}
check() { # label condition-result(0/1)
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; else echo "FAIL: $1"; FAILURES=$((FAILURES+1)); fi
}

echo "=== 1. Server A: monico-server start --name linux-a (port $PA) ==="
MONICO_HOME="$A_HOME" python3 "$REPO/monico-server" start --port $PA \
  --name linux-a > /tmp/mesh-a.log 2>&1 &
A_PID=$!
for i in $(seq 1 30); do curl -s "http://127.0.0.1:$PA/health" >/dev/null 2>&1 && break; sleep 0.5; done
curl -s "http://127.0.0.1:$PA/health"; echo
KEY="$(MONICO_HOME="$A_HOME" python3 "$REPO/monico-server" key | awk '{print $NF}')"
echo "pairing key acquired (length ${#KEY})"

echo "=== 2. Server B: monico-server start --name linux-b --join A (port $PB) ==="
MONICO_HOME="$B_HOME" python3 "$REPO/monico-server" start --port $PB \
  --name linux-b --join "http://127.0.0.1:$PA" --key "$KEY" > /tmp/mesh-b.log 2>&1 &
B_PID=$!
wait_until $PA linux-b online 40; check "B registered + online on A" $?
wait_until $PB linux-a online 40; check "B sees A (sync flowed back)" $?

echo "=== 3. Simulated Termux clients: T1 -> A, T2 -> B ==="
HOME="$T1_HOME" MONICO_HOME="$T1_HOME/.monico-bridge" MONICO_SERVICE_PORT=8921 \
  python3 "$REPO/monico-connect" --server "http://127.0.0.1:$PA" --key "$KEY" \
  --name termux-t1 > /tmp/mesh-t1.log 2>&1 &
T1_PID=$!
HOME="$T2_HOME" MONICO_HOME="$T2_HOME/.monico-bridge" MONICO_SERVICE_PORT=8922 \
  python3 "$REPO/monico-connect" --server "http://127.0.0.1:$PB" --key "$KEY" \
  --name termux-t2 > /tmp/mesh-t2.log 2>&1 &
T2_PID=$!
wait_until $PA termux-t1 online 40; check "T1 online on A" $?
wait_until $PA termux-t2 online 60; check "T2 (on B) visible + online on A" $?
wait_until $PB termux-t1 online 60; check "T1 (on A) visible + online on B" $?

echo "=== 4. (a) Shared device list: homescreen data on A and on B ==="
echo "--- A /api/devices ---"; show_devices $PA
echo "--- B /api/devices ---"; show_devices $PB
N_A=$(devs $PA | python3 -c "import json,sys; print(len(json.load(sys.stdin)['devices']))")
N_B=$(devs $PB | python3 -c "import json,sys; print(len(json.load(sys.stdin)['devices']))")
echo "device count: A=$N_A B=$N_B (expect 4 and 4)"
[ "$N_A" = 4 ] && [ "$N_B" = 4 ]; check "both servers list exactly the 4 mesh nodes" $?
CODE=$(curl -s -o /tmp/mesh-home.html -w "%{http_code}" -H "X-Pair-Key: $KEY" "http://127.0.0.1:$PA/")
echo "homescreen on A: HTTP $CODE, banner: $(grep -o 'MONICO LABS' /tmp/mesh-home.html | head -1)"
[ "$CODE" = 200 ]; check "homescreen loads on A" $?

echo "=== 5. (b) Command issued ON T1 executes ON T2 (cross-server relay) ==="
HOME="$T1_HOME" MONICO_HOME="$T1_HOME/.monico-bridge" \
  python3 "$REPO/monico-cmd" termux-t2 'echo RELAY_T1_TO_T2_OK; echo "ran in env MONICO_HOME=$MONICO_HOME"'
echo "exit=$? (marker env must be T2's: $T2_HOME/.monico-bridge)"

echo "=== 6. (c) Commands from server A execute on T2 and on B ==="
MONICO_SERVER="http://127.0.0.1:$PA" MONICO_TOKEN="$KEY" MONICO_HOME="$A_HOME" \
  python3 "$REPO/monico-cmd" termux-t2 'echo A_TO_T2_OK; echo "ran in env MONICO_HOME=$MONICO_HOME"'
echo "exit=$?"
MONICO_SERVER="http://127.0.0.1:$PA" MONICO_TOKEN="$KEY" MONICO_HOME="$A_HOME" \
  python3 "$REPO/monico-cmd" linux-b 'echo ON_LINUX_B_OK; echo "ran in env MONICO_HOME=$MONICO_HOME"'
echo "exit=$? (marker env must be B's: $B_HOME)"

echo "=== 7. (d) Kill B: T2 + B go offline on A, A keeps working ==="
kill $B_PID 2>/dev/null; B_PID=""
sleep 1
wait_until $PA termux-t2 offline 90; check "T2 marked offline on A after B died" $?
wait_until $PA linux-b offline 90; check "linux-b marked offline on A" $?
echo "--- A /api/devices after B died ---"; show_devices $PA
MONICO_SERVER="http://127.0.0.1:$PA" MONICO_TOKEN="$KEY" MONICO_HOME="$A_HOME" \
  python3 "$REPO/monico-cmd" linux 'echo A_STILL_OK'
[ $? -eq 0 ]; check "A still executes local commands" $?
curl -s "http://127.0.0.1:$PA/health" >/dev/null; check "A health endpoint still answers" $?

echo "=== 8. (e) Restart B (no --key: adopted key persisted): mesh heals ==="
MONICO_HOME="$B_HOME" python3 "$REPO/monico-server" start --port $PB \
  --name linux-b --join "http://127.0.0.1:$PA" > /tmp/mesh-b2.log 2>&1 &
B_PID=$!
wait_until $PA linux-b online 60; check "linux-b back online on A" $?
wait_until $PA termux-t2 online 90; check "T2 back online on A (rejoin heals)" $?
echo "--- A /api/devices after rejoin ---"; show_devices $PA

echo "=== mesh proof done: $FAILURES failure(s) ==="
exit $FAILURES
