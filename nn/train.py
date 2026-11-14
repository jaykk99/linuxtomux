"""Train the fit network on the real (device, model, quant) triples.

    python3 -m nn.train [--out DIR] [--epochs N] [--hidden 8]

Trains an MLP to predict whether a catalog (model, quant) fits a
device's budget under pick()'s rule. Writes weights.json +
viewer-data.json into the out dir (default: this package dir) and
refreshes the embedded metrics block in viewer.html.

Honest limits, printed with the metrics and baked into the viewer:
- 54 rows only (6 devices x 9 catalog combos); the test set is 10
  rows, so every test metric has a wide error bar.
- the labels are pick()'s own rule applied to the real catalog and
  the real device records. A high score means the network reproduced
  that threshold, not that it discovered anything new. 100% test
  accuracy is the overfit/rule-reproduction flag, not a win.
- the model never installs anything and never overrides pick():
  pick() stays the authority; this is a pre-check / ranker only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .data import build_dataset, stratified_split
from .features import extract, feature_names
from .mlp import MLP, metrics_for, roc_auc

LIMITS = [
    "Pre-check only. The model predicts whether a (model, quant) fits "
    "a device's budget; it never installs anything and never overrides "
    "pick() in bridge/model_installer.py, which stays the authority.",
    "Labels are rule-derived: each row is labeled by pick()'s own "
    "budget rule (size <= min(usable/1.25, disk_free*0.5)) applied to "
    "the real CATALOG and the real device records. A high score means "
    "the network reproduced that threshold, not a discovery.",
    # Index 2 is replaced at train time with the real counts.
    "Small data: see the dataset stats.",
    "Devices unseen in training (new RAM/disk numbers) are "
    "extrapolations; the model has no opinion beyond the raw features.",
]

VIEWER_START = "<!-- NN-DATA-START -->"
VIEWER_END = "<!-- NN-DATA-END -->"


def permutation_importance(net: MLP, X_test, y_test, seed: int = 42):
    """Per-feature permutation importance: AUC drop when a feature is
    shuffled in the test set. Positive drop = the feature matters."""
    import random
    base_scores = [net.predict_proba(x) for x in X_test]
    base_auc = roc_auc(base_scores, y_test)
    rng = random.Random(seed)
    out = []
    names = feature_names()
    for i, name in enumerate(names):
        Xp = [list(x) for x in X_test]
        col = [x[i] for x in Xp]
        rng.shuffle(col)
        for row, v in zip(Xp, col):
            row[i] = v
        auc = roc_auc([net.predict_proba(x) for x in Xp], y_test)
        out.append({"feature": name, "auc_drop": round(base_auc - auc, 4)})
    out.sort(key=lambda d: d["auc_drop"], reverse=True)
    return out


def train(out_dir: str, epochs: int = 150, hidden: str = "8",
          lr: float = 0.05, seed: int = 42,
          test_frac: float = 0.2) -> dict:
    dataset = build_dataset()
    rows = dataset["rows"]
    stats = dataset["stats"]
    if len(rows) < 20:
        raise ValueError(f"only {len(rows)} training rows; refusing")
    train_rows, test_rows = stratified_split(rows, test_frac, seed)
    X_train = [extract(r) for r in train_rows]
    y_train = [r["label"] for r in train_rows]
    X_test = [extract(r) for r in test_rows]
    y_test = [r["label"] for r in test_rows]

    limits = list(LIMITS)
    limits[2] = (
        f"Small data: {len(rows)} rows (6 devices x 9 catalog combos), "
        f"{stats['n_positive']} fit / {stats['n_negative']} no-fit. "
        f"Test set is {len(test_rows)} rows, so one test row is "
        f"{100.0 / max(1, len(test_rows)):.0f}% of test accuracy. "
        "Judge it by ROC AUC and the confusion counts, not accuracy.")

    layers = [len(feature_names())] + [int(h) for h in hidden.split(",")
                                        if h] + [1]
    net = MLP(layers, seed=seed, lr=lr)
    history = net.fit(X_train, y_train, epochs=epochs, batch_size=16,
                      X_val=X_test, y_val=y_test, seed=seed)
    train_scores = [net.predict_proba(x) for x in X_train]
    test_scores = [net.predict_proba(x) for x in X_test]
    train_metrics = metrics_for(y_train, train_scores)
    test_metrics = metrics_for(y_test, test_scores)
    importance = permutation_importance(net, X_test, y_test, seed)

    result = {
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "layers": layers,
        "n_features": len(feature_names()),
        "feature_names": feature_names(),
        "dataset": stats,
        "train_rows": len(train_rows),
        "test_rows": len(test_rows),
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "history": history,
        "feature_importance": importance,
        "limits": limits,
        "seed": seed,
        "epochs": epochs,
        "lr": lr,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    weights = {
        "format": "linuxtomux-nn-weights/1",
        "trained_at": result["trained_at"],
        "mlp": net.to_dict(),
        "metrics": {"train": train_metrics, "test": test_metrics},
        "dataset": stats,
        "limits": limits,
    }
    (out / "weights.json").write_text(json.dumps(weights, indent=2),
                                      encoding="utf-8")
    (out / "viewer-data.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    _refresh_viewer(out / "viewer.html", result)
    return result


def _refresh_viewer(viewer_path: Path, result: dict) -> None:
    if not viewer_path.exists():
        return
    text = viewer_path.read_text(encoding="utf-8")
    if VIEWER_START not in text or VIEWER_END not in text:
        return
    block = (VIEWER_START + "\n<script type=\"application/json\" "
             "id=\"nn-data\">\n" + json.dumps(result, indent=2) +
             "\n</script>\n" + VIEWER_END)
    before, _sep, rest = text.partition(VIEWER_START)
    _mid, _sep2, after = rest.partition(VIEWER_END)
    viewer_path.write_text(before + block + after, encoding="utf-8")


def _print_report(result: dict) -> None:
    tm = result["test_metrics"]
    tr = result["train_metrics"]
    print(f"rows: train={result['train_rows']} test={result['test_rows']} "
          f"(fit {result['dataset']['n_positive']} / "
          f"no-fit {result['dataset']['n_negative']})")
    print(f"features: {result['n_features']}  layers: {result['layers']}")
    print(f"train acc {tr['accuracy']:.3f}  test acc {tm['accuracy']:.3f}  "
          f"majority baseline {tm['majority_baseline_accuracy']:.3f}")
    print(f"test ROC AUC {tm['roc_auc']:.3f}")
    print(f"fits: precision {tm['precision_fits']:.3f} "
          f"recall {tm['recall_fits']:.3f}")
    print(f"no-fit: precision {tm['precision_no_fit']:.3f} "
          f"recall {tm['recall_no_fit']:.3f}")
    cm = tm["confusion"]
    print(f"confusion (test): tp={cm['tp']} fp={cm['fp']} "
          f"fn={cm['fn']} tn={cm['tn']}")
    print("feature importance (AUC drop): " + ", ".join(
        f"{d['feature']}={d['auc_drop']:.3f}"
        for d in result["feature_importance"][:5]))
    if tm["accuracy"] >= 1.0:
        print("FLAG: test accuracy is 100% — the network reproduced "
              "pick()'s threshold rule on 11 test rows. That is the "
              "expected rule-reproduction outcome, not a win; see limits.")
    print("LIMITS: pre-check only; labels are pick()'s rule; pick() "
          "stays the authority.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Train the linuxtomux fit network.")
    ap.add_argument("--out",
                    default=str(Path(__file__).resolve().parent))
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--hidden", default="8")
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--test-frac", type=float, default=0.2)
    args = ap.parse_args(argv)
    result = train(args.out, epochs=args.epochs, hidden=args.hidden,
                   lr=args.lr, seed=args.seed, test_frac=args.test_frac)
    _print_report(result)
    print(f"weights: {Path(args.out) / 'weights.json'}")
    print(f"viewer data: {Path(args.out) / 'viewer-data.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
