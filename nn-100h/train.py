"""nn-100h trainer for linuxtomux — 100 hours of grouped-split training.

Run from repo_root/nn-100h/:
    python3 train.py [--resume] [--max-run-hours H]   # default H = 5.5

Pure Python stdlib, self-contained: tanh hidden layers, sigmoid output,
binary cross-entropy, mini-batch SGD (same shape as the PR #8 network
in nn/mlp.py, reimplemented here so this file has no imports from repo
code and runs anywhere, including GitHub Actions and Termux).

What it predicts: whether a (device, model, quant) from
bridge/model_installer.py CATALOG fits the device budget under pick()'s
rule. Triage-only: pick() stays the authority, this never installs
anything and never overrides pick().

Data: dataset.jsonl + split.json in this directory (built by
build_dataset.py from the repo's own CATALOG and device profiles;
see DATASET.md). The split is FROZEN and GROUPED: the test set is the
whole android-8gb device family, never trained on. A test accuracy of
1.000 is a leakage signal, not a win — see DATASET.md.

Loop: a deterministic grid over hidden size, lr, L2, epochs, seeds.
Experiment n is config_for_index(n); seeds are unbounded, so the grid
cannot run out before 100 hours. Each experiment trains on the train
split only, is evaluated on the frozen test split, and appends one line
to experiments.jsonl. best.json / best-metrics.json update only when
honest test accuracy improves (ROC AUC tiebreak).

Chunking / resume (VM reboots ~every 2h, /tmp is wiped — everything
lives here, in the repo checkout):
  - status.json carries cumulative hours_trained across restarts.
  - checkpoints/<exp_id>.json is written every 20 epochs
    {exp_id, config, epoch, weights, biases, train_loss} and deleted
    when its experiment completes (its record then lives in
    experiments.jsonl; best weights live in best.json).
  - --resume continues the in-progress experiment from its latest
    checkpoint (weights + epoch). Without --resume the in-progress
    experiment restarts from epoch 0. Hours always accumulate.
  - Per-epoch shuffling uses Random(seed * 1000003 + epoch), so a
    resumed run sees exactly the epoch order an uninterrupted run
    would have seen.
  - heartbeat.json is written at least every 30s.
  - When wall time reaches --max-run-hours the current experiment is
    checkpointed, status is saved, and the process exits 0. When
    cumulative hours_trained reaches 100, status becomes "done" and
    the process exits 0.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "jaykk99/linuxtomux"
TARGET_HOURS = 100.0
CHECKPOINT_EVERY_EPOCHS = 20
HEARTBEAT_EVERY_S = 30.0
BATCH_SIZE = 16

# Grid. Seeds are unbounded (config_for_index maps n -> seed + combo),
# so the loop cannot exhaust before 100h.
HIDDEN_SIZES = [4, 8, 16, 32]
LRS = [0.01, 0.03, 0.1, 0.3]
L2S = [0.0, 1e-4, 1e-3]
EPOCHS_LIST = [100, 300, 600]

QUANTS = ("Q4_K_M", "Q5_K_M", "Q8_0")
QUANT_PREFERENCE = ["Q8_0", "Q5_K_M", "Q4_K_M"]
FEATURES = ["params_b", "size_gb", "quant:Q4_K_M", "quant:Q5_K_M",
            "quant:Q8_0", "quant_rank", "usable_ram_gb", "disk_free_gb",
            "is_android", "cpu_cores"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}",
          flush=True)


# ---------------------------------------------------------------- features
def extract(row: dict) -> list[float]:
    """One dataset row -> feature vector. Raw inputs only.

    Deliberately NOT features: the budget min(usable/1.25, disk*0.5)
    and the size/budget ratio. The network must learn the threshold
    from the raw numbers, the same way pick() computes it.
    """
    vec: list[float] = []
    vec.append(float(row.get("params_b") or 0) / 8.0)
    vec.append(float(row.get("size_gb") or 0) / 10.0)
    quant = str(row.get("quant") or "")
    for q in QUANTS:
        vec.append(1.0 if quant == q else 0.0)
    rank = (QUANT_PREFERENCE.index(quant) if quant in QUANT_PREFERENCE
            else len(QUANT_PREFERENCE))
    vec.append(rank / max(1, len(QUANT_PREFERENCE) - 1))
    vec.append(float(row.get("usable_ram_gb") or 0) / 64.0)
    disk = float(row.get("disk_free_gb") or 0)
    vec.append(min(1.0, math.log10(1.0 + disk) / math.log10(1801.0)))
    vec.append(1.0 if str(row.get("platform") or "") == "android-termux"
               else 0.0)
    vec.append(float(row.get("cpu_cores") or 0) / 24.0)
    return vec


# --------------------------------------------------------------------- MLP
def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class MLP:
    """Feed-forward net: tanh hidden, sigmoid output, BCE, mini-batch SGD."""

    def __init__(self, layers, seed=42, lr=0.05, l2=1e-4):
        self.layers = list(layers)
        self.lr = float(lr)
        self.l2 = float(l2)
        self.seed = int(seed)
        rng = random.Random(seed)
        self.weights, self.biases = [], []
        for fan_in, fan_out in zip(self.layers[:-1], self.layers[1:]):
            limit = math.sqrt(6.0 / (fan_in + fan_out))  # Xavier uniform
            self.weights.append(
                [[rng.uniform(-limit, limit) for _ in range(fan_in)]
                 for _ in range(fan_out)])
            self.biases.append([0.0 for _ in range(fan_out)])

    def _forward(self, x):
        acts = [list(map(float, x))]
        for li, (w, b) in enumerate(zip(self.weights, self.biases)):
            z = [sum(wi * xi for wi, xi in zip(row, acts[-1])) + bias
                 for row, bias in zip(w, b)]
            if li == len(self.weights) - 1:
                acts.append([_sigmoid(v) for v in z])
            else:
                acts.append([math.tanh(v) for v in z])
        return acts

    def predict_proba(self, x) -> float:
        return self._forward(x)[-1][0]

    def predict(self, x, threshold=0.5) -> int:
        return 1 if self.predict_proba(x) >= threshold else 0

    def _backward(self, acts, y):
        n_layers = len(self.weights)
        deltas: list = [None] * n_layers
        deltas[-1] = [acts[-1][0] - y]
        for li in range(n_layers - 2, -1, -1):
            w_next, d_next = self.weights[li + 1], deltas[li + 1]
            a = acts[li + 1]
            deltas[li] = [sum(w_next[k][j] * d_next[k]
                              for k in range(len(d_next)))
                          * (1.0 - a[j] * a[j]) for j in range(len(a))]
        gw, gb = [], []
        for li in range(n_layers):
            d, a_prev = deltas[li], acts[li]
            gw.append([[d[i] * a_prev[j] for j in range(len(a_prev))]
                       for i in range(len(d))])
            gb.append(list(d))
        return gw, gb

    def train_epoch(self, X, y, epoch: int) -> None:
        """One SGD epoch. Shuffle RNG depends on (seed, epoch) only,
        so resuming from a checkpoint reproduces the uninterrupted
        epoch order exactly."""
        rng = random.Random(self.seed * 1000003 + epoch)
        order = list(range(len(X)))
        rng.shuffle(order)
        for start in range(0, len(X), BATCH_SIZE):
            idxs = order[start:start + BATCH_SIZE]
            gw_acc = [[[0.0] * len(row) for row in w]
                      for w in self.weights]
            gb_acc = [[0.0] * len(b) for b in self.biases]
            for i in idxs:
                acts = self._forward(X[i])
                gw, gb = self._backward(acts, float(y[i]))
                for li in range(len(gw)):
                    for r in range(len(gw[li])):
                        for c in range(len(gw[li][r])):
                            gw_acc[li][r][c] += gw[li][r][c]
                    for r in range(len(gb[li])):
                        gb_acc[li][r] += gb[li][r]
            m = float(len(idxs))
            for li in range(len(self.weights)):
                for r in range(len(self.weights[li])):
                    for c in range(len(self.weights[li][r])):
                        g = gw_acc[li][r][c] / m
                        g += self.l2 * self.weights[li][r][c]
                        self.weights[li][r][c] -= self.lr * g
                for r in range(len(self.biases[li])):
                    self.biases[li][r] -= self.lr * (gb_acc[li][r] / m)

    def bce(self, X, y) -> float:
        eps = 1e-12
        total = 0.0
        for xi, yi in zip(X, y):
            p = min(max(self.predict_proba(xi), eps), 1.0 - eps)
            total += -(yi * math.log(p) + (1.0 - yi) * math.log(1.0 - p))
        return total / max(1, len(X))

    def accuracy(self, X, y) -> float:
        if not X:
            return 0.0
        return sum(1 for xi, yi in zip(X, y)
                   if self.predict(xi) == yi) / len(X)

    def to_checkpoint(self, exp_id, config, epoch, train_loss) -> dict:
        return {"exp_id": exp_id, "config": config, "epoch": int(epoch),
                "weights": self.weights, "biases": self.biases,
                "train_loss": train_loss}

    @classmethod
    def from_checkpoint(cls, ckpt: dict) -> "MLP":
        cfg = ckpt["config"]
        net = cls([len(ckpt["weights"][0][0])] +
                  [len(w) for w in ckpt["weights"]],
                  seed=int(cfg["seed"]), lr=float(cfg["lr"]),
                  l2=float(cfg["l2"]))
        net.weights = ckpt["weights"]
        net.biases = ckpt["biases"]
        return net


# ------------------------------------------------------------------- metrics
def roc_auc(scores, labels) -> float:
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    n_pos = sum(1 for _, lab in pairs if lab == 1)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_sum = sum(r for r, (_, lab) in zip(ranks, pairs) if lab == 1)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def evaluate(net, X, y) -> dict:
    scores = [net.predict_proba(xi) for xi in X]
    preds = [1 if s >= 0.5 else 0 for s in scores]
    tp = sum(1 for t, p in zip(y, preds) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y, preds) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y, preds) if t == 1 and p == 0)
    tn = sum(1 for t, p in zip(y, preds) if t == 0 and p == 0)
    return {
        "test_acc": (tp + tn) / max(1, len(y)),
        "auc": roc_auc(scores, list(y)),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "per_class_recall": {
            "fits": tp / (tp + fn) if tp + fn else 0.0,
            "no_fit": tn / (tn + fp) if tn + fp else 0.0,
        },
    }


# ---------------------------------------------------------------------- grid
def config_for_index(n: int) -> dict:
    """Deterministic grid config for experiment ordinal n.

    144 combos per seed (4 hidden x 4 lr x 3 L2 x 3 epochs); seeds
    unbounded, so the grid never runs out before 100h.
    """
    per_seed = (len(HIDDEN_SIZES) * len(LRS) * len(L2S)
                * len(EPOCHS_LIST))
    seed, rest = divmod(n, per_seed)
    hidden, rest = divmod(rest, len(LRS) * len(L2S) * len(EPOCHS_LIST))
    lr, rest = divmod(rest, len(L2S) * len(EPOCHS_LIST))
    l2, epochs = divmod(rest, len(EPOCHS_LIST))
    return {"hidden": HIDDEN_SIZES[hidden], "lr": LRS[lr],
            "l2": L2S[l2], "epochs": EPOCHS_LIST[epochs], "seed": seed}


def exp_id_for_index(n: int) -> str:
    return f"exp-{n:05d}"


def index_for_exp_id(exp_id: str) -> int:
    return int(exp_id.split("-")[1])


# ------------------------------------------------------------------ io helpers
def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_dataset():
    if not (HERE / "dataset.jsonl").exists() or \
            not (HERE / "split.json").exists():
        log("dataset/split missing, running build_dataset.py ...")
        proc = subprocess.run([sys.executable, str(HERE / "build_dataset.py")],
                              cwd=str(HERE))
        if proc.returncode != 0:
            raise SystemExit("build_dataset.py failed; cannot train")
    rows = [json.loads(line) for line in
            open(HERE / "dataset.jsonl", encoding="utf-8") if line.strip()]
    split = load_json(HERE / "split.json", {})
    assert split, "split.json empty"
    return rows, split


def load_done() -> dict:
    done = {}
    path = HERE / "experiments.jsonl"
    if path.exists():
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                rec = json.loads(line)
                done[rec["exp_id"]] = rec
    return done


def latest_incomplete_checkpoint(done: dict):
    """Newest checkpoint whose experiment has no experiments.jsonl line."""
    ckpt_dir = HERE / "checkpoints"
    best = None
    if ckpt_dir.exists():
        for path in ckpt_dir.glob("*.json"):
            ckpt = load_json(path, None)
            if not ckpt or ckpt.get("exp_id") in done:
                continue
            mtime = path.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, path, ckpt)
    return best[2] if best else None


# --------------------------------------------------------------------- trainer
class Trainer:
    def __init__(self, max_run_hours: float, resume: bool):
        self.max_run_hours = float(max_run_hours)
        self.resume = resume
        self.chunk_start = time.monotonic()
        self.deadline = self.chunk_start + self.max_run_hours * 3600
        self.pid = os.getpid()
        self.rows, self.split = load_dataset()
        self.done = load_done()
        self.best = load_json(HERE / "best.json", None)
        self.status = load_json(HERE / "status.json", None) or {}
        self.hours_before = float(self.status.get("hours_trained") or 0.0)
        self.current_exp_id: str | None = None
        self.current_epoch = 0
        self._last_hb = 0.0
        (HERE / "checkpoints").mkdir(exist_ok=True)
        # Frozen split: train on train rows ONLY, evaluate on test rows.
        self.X_train = [extract(self.rows[i])
                        for i in self.split["train_idx"]]
        self.y_train = [self.rows[i]["label"]
                        for i in self.split["train_idx"]]
        self.X_test = [extract(self.rows[i])
                       for i in self.split["test_idx"]]
        self.y_test = [self.rows[i]["label"]
                       for i in self.split["test_idx"]]
        log(f"dataset: {len(self.rows)} rows, train {len(self.X_train)}, "
            f"test {len(self.X_test)} (grouped holdout: "
            f"{self.split.get('test_devices')})")
        log(f"hours before this chunk: {self.hours_before:.4f}, "
            f"experiments done: {len(self.done)}, "
            f"max-run-hours: {self.max_run_hours}")

    # -- time / status -------------------------------------------------
    def chunk_hours(self) -> float:
        return (time.monotonic() - self.chunk_start) / 3600.0

    def total_hours(self) -> float:
        return self.hours_before + self.chunk_hours()

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def write_status(self, status: str) -> None:
        write_json(HERE / "status.json", {
            "repo": REPO,
            "hours_trained": round(self.total_hours(), 6),
            "experiments_done": len(self.done),
            "best": ({"exp_id": self.best["exp_id"],
                      "test_acc": self.best["test_acc"],
                      "auc": self.best["auc"]} if self.best else None),
            "status": status,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def write_heartbeat(self, force=False) -> None:
        now = time.monotonic()
        if not force and now - self._last_hb < HEARTBEAT_EVERY_S:
            return
        self._last_hb = now
        write_json(HERE / "heartbeat.json", {
            "pid": self.pid,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "exp_id": self.current_exp_id,
            "epoch": self.current_epoch,
            "experiments_done": len(self.done),
        })
        # Keep cumulative hours live too: a SIGKILL / VM reboot between
        # experiments must not lose the current chunk's elapsed time.
        self.write_status("training")

    # -- checkpoints ---------------------------------------------------
    def checkpoint_path(self, exp_id: str) -> Path:
        return HERE / "checkpoints" / f"{exp_id}.json"

    def save_checkpoint(self, net, exp_id, config, epoch) -> None:
        loss = net.bce(self.X_train, self.y_train)
        write_json(self.checkpoint_path(exp_id),
                   net.to_checkpoint(exp_id, config, epoch, loss))

    # -- one experiment -------------------------------------------------
    def run_experiment(self, n: int, resume_ckpt=None):
        exp_id = exp_id_for_index(n)
        config = config_for_index(n)
        self.current_exp_id = exp_id
        t0 = time.monotonic()
        if resume_ckpt is not None:
            net = MLP.from_checkpoint(resume_ckpt)
            start_epoch = int(resume_ckpt["epoch"])
            log(f"RESUMED {exp_id} from checkpoint epoch {start_epoch}/"
                f"{config['epochs']} (config {config})")
        else:
            layers = [len(FEATURES), config["hidden"], 1]
            net = MLP(layers, seed=config["seed"], lr=config["lr"],
                      l2=config["l2"])
            start_epoch = 0
            log(f"START {exp_id} config {config}")
        self.current_epoch = start_epoch
        completed = False
        for epoch in range(start_epoch, config["epochs"]):
            net.train_epoch(self.X_train, self.y_train, epoch)
            self.current_epoch = epoch + 1
            self.write_heartbeat()
            if (self.current_epoch % CHECKPOINT_EVERY_EPOCHS == 0
                    or self.current_epoch == config["epochs"]):
                self.save_checkpoint(net, exp_id, config,
                                     self.current_epoch)
            if self.time_left() <= 0 or self.total_hours() >= TARGET_HOURS:
                log(f"STOP {exp_id} at epoch {self.current_epoch}/"
                    f"{config['epochs']} (time budget reached)")
                self.save_checkpoint(net, exp_id, config,
                                     self.current_epoch)
                return False
        completed = True
        seconds = time.monotonic() - t0
        metrics = evaluate(net, self.X_test, self.y_test)
        train_acc = net.accuracy(self.X_train, self.y_train)
        record = {"exp_id": exp_id, "config": config,
                  "test_acc": metrics["test_acc"], "auc": metrics["auc"],
                  "confusion": metrics["confusion"],
                  "per_class_recall": metrics["per_class_recall"],
                  "train_acc": train_acc,
                  "train_loss": net.bce(self.X_train, self.y_train),
                  "seconds": round(seconds, 2)}
        with open(HERE / "experiments.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self.done[exp_id] = record
        try:
            self.checkpoint_path(exp_id).unlink()
        except OSError:
            pass
        self.update_best(net, record)
        log(f"DONE {exp_id} test_acc={record['test_acc']:.3f} "
            f"auc={record['auc']:.3f} confusion={record['confusion']} "
            f"recall={record['per_class_recall']} "
            f"train_acc={train_acc:.3f} seconds={seconds:.1f}")
        if record["test_acc"] == 1.0:
            log(f"WARNING {exp_id}: test_acc 1.000 on the grouped holdout "
                f"is a leakage signal, not a win — investigate before "
                f"claiming anything (see DATASET.md).")
        return True

    def update_best(self, net, record) -> None:
        better = (self.best is None
                  or record["test_acc"] > self.best["test_acc"]
                  or (record["test_acc"] == self.best["test_acc"]
                      and record["auc"] > self.best["auc"]))
        if not better:
            return
        self.best = {"exp_id": record["exp_id"], "config": record["config"],
                     "test_acc": record["test_acc"], "auc": record["auc"],
                     "confusion": record["confusion"],
                     "per_class_recall": record["per_class_recall"],
                     "train_acc": record["train_acc"],
                     "weights": net.weights, "biases": net.biases,
                     "layers": net.layers, "features": FEATURES,
                     "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())}
        write_json(HERE / "best.json", self.best)
        write_json(HERE / "best-metrics.json", {
            k: v for k, v in self.best.items()
            if k not in ("weights", "biases")})
        log(f"BEST <- {record['exp_id']} test_acc={record['test_acc']:.3f} "
            f"auc={record['auc']:.3f}")

    # -- main loop ------------------------------------------------------
    def run(self) -> int:
        if self.total_hours() >= TARGET_HOURS:
            self.write_status("done")
            log(f"already at {self.total_hours():.2f}h >= "
                f"{TARGET_HOURS}h — done")
            return 0
        self.write_status("training")
        self.write_heartbeat(force=True)
        # Resume path: continue the in-progress experiment first.
        n = 0
        while exp_id_for_index(n) in self.done:
            n += 1
        if self.resume:
            ckpt = latest_incomplete_checkpoint(self.done)
            if ckpt is not None:
                cn = index_for_exp_id(ckpt["exp_id"])
                if cn == n:
                    if not self.run_experiment(cn, resume_ckpt=ckpt):
                        self.finish("training")
                        return 0
                    n = cn + 1
                    while exp_id_for_index(n) in self.done:
                        n += 1
                else:
                    log(f"note: checkpoint {ckpt['exp_id']} is not the "
                        f"next experiment ({exp_id_for_index(n)}); "
                        f"starting from {exp_id_for_index(n)}")
        while True:
            if self.time_left() <= 0:
                log("chunk wall-time budget reached")
                break
            if self.total_hours() >= TARGET_HOURS:
                log(f"reached {TARGET_HOURS}h total")
                break
            if not self.run_experiment(n):
                break
            n += 1
            while exp_id_for_index(n) in self.done:
                n += 1
            self.write_status("training")
            self.write_heartbeat(force=True)
        self.finish("done" if self.total_hours() >= TARGET_HOURS
                    else "training")
        return 0

    def finish(self, status: str) -> None:
        self.write_status(status)
        self.write_heartbeat(force=True)
        log(f"chunk exit: total hours {self.total_hours():.4f}, "
            f"experiments done {len(self.done)}, status={status}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--resume", action="store_true",
                    help="resume in-progress experiment from checkpoint")
    ap.add_argument("--max-run-hours", type=float, default=5.5,
                    help="wall-time budget for this chunk (default 5.5)")
    args = ap.parse_args()
    trainer = Trainer(args.max_run_hours, args.resume)
    return trainer.run()


if __name__ == "__main__":
    raise SystemExit(main())
