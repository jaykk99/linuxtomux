#!/usr/bin/env bash
# End-to-end proof on this VM: Linux server + simulated Termux client
# (a clean shell standing in for the phone; real Android Termux cannot
# run on this VM). Writes everything to proof/proof-output.txt.
set -u
cd "$(dirname "$0")/.."
REPO="$PWD"
export PYTHONPATH="$REPO"
PORT=8877
BASE="http://127.0.0.1:$PORT"
export MONICO_HOME=/tmp/monico-proof-home
PHONE_HOME=/tmp/monico-proof-phonehome
OUT="$REPO/proof/proof-output.txt"
mkdir -p "$REPO/proof"
exec > >(tee "$OUT") 2>&1

rm -rf "$MONICO_HOME" "$PHONE_HOME"
mkdir -p "$MONICO_HOME" "$PHONE_HOME/monico-host"
echo "hosted-on-the-phone proof file" > "$PHONE_HOME/monico-host/hello.txt"

cleanup() { kill ${SRV_PID:-} ${CLI_PID:-} 2>/dev/null; }
trap cleanup EXIT

echo "=== 1. Linux side: monico-server start (port $PORT) ==="
python3 "$REPO/monico-server" start --port $PORT > /tmp/monico-proof-server.log 2>&1 &
SRV_PID=$!
for i in $(seq 1 30); do curl -s "$BASE/health" >/dev/null 2>&1 && break; sleep 0.5; done
curl -s "$BASE/health"; echo
KEY="$(python3 "$REPO/monico-server" key | awk '{print $NF}')"
echo "pairing key acquired (length ${#KEY})"

echo "=== 2. Termux side (simulated shell): monico-connect ==="
HOME="$PHONE_HOME" python3 "$REPO/monico-connect" --server "$BASE" --key "$KEY" \
  --name termux-sim > /tmp/monico-proof-client.log 2>&1 &
CLI_PID=$!
for i in $(seq 1 40); do
  curl -s -H "X-Pair-Key: $KEY" "$BASE/api/devices" | grep -q termux-sim && break
  sleep 0.5
done
curl -s -H "X-Pair-Key: $KEY" "$BASE/api/devices" | python3 -m json.tool

echo "=== 3. Linux -> phone command ==="
python3 "$REPO/monico-cmd" phone 'echo PHONE_SIDE_OK && uname -s && whoami'
echo "exit=$?"

echo "=== 4. Phone -> Linux command ==="
mkdir -p "$PHONE_HOME/.monico-bridge"
cp "$MONICO_HOME/client.json" "$PHONE_HOME/.monico-bridge/client.json"
MONICO_HOME="$PHONE_HOME/.monico-bridge" HOME="$PHONE_HOME" \
  python3 "$REPO/monico-cmd" linux 'echo LINUX_SIDE_OK && hostname'
echo "exit=$?"

echo "=== 5. Homescreen (owner key) ==="
CODE=$(curl -s -o /tmp/monico-proof-home.html -w "%{http_code}" -H "X-Pair-Key: $KEY" "$BASE/")
echo "HTTP $CODE"
grep -o "MONICO LABS" /tmp/monico-proof-home.html | head -1
grep -c "tile" /tmp/monico-proof-home.html
echo "--- homescreen without key (must be login page) ---"
curl -s "$BASE/" | grep -o "Owner access" | head -1

echo "=== 6. Phone hosted service, proxied through the bridge ==="
DEVID=$(curl -s -H "X-Pair-Key: $KEY" "$BASE/api/devices" | python3 -c "import json,sys; print([d['device_id'] for d in json.load(sys.stdin)['devices'] if d['device_id']!='linux'][0])")
echo "device_id=$DEVID"
curl -s -H "X-Pair-Key: $KEY" "$BASE/device/$DEVID/hosted/status.json" | python3 -m json.tool | head -20
curl -s -H "X-Pair-Key: $KEY" "$BASE/device/$DEVID/hosted/files/hello.txt"; echo

echo "=== 7. Model installer dry runs (tier picks by hardware) ==="
python3 "$REPO/monico-model" install --dry-run --profile galaxy-s22 | \
  python3 -c "import json,sys; d=json.load(sys.stdin); p=d['pick']; print('galaxy-s22 ->', p['model'], p['quant'], p['size_gb'], 'GB, budget', p['budget_gb'], 'GB')"
python3 "$REPO/monico-model" install --dry-run --profile linux-box-32gb | \
  python3 -c "import json,sys; d=json.load(sys.stdin); p=d['pick']; print('linux-32gb  ->', p['model'], p['quant'], p['size_gb'], 'GB, budget', p['budget_gb'], 'GB')"
python3 "$REPO/monico-model" install --dry-run | \
  python3 -c "import json,sys; d=json.load(sys.stdin); p=d['pick']; h=d['device_profile']; print('this VM     ->', p['model'], p['quant'], p['size_gb'], 'GB | detected RAM total/avail GB:', h['ram_total_gb'], h['ram_available_gb'])"

echo "=== proof done ==="
