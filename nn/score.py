"""Score a (device, model, quant) triple with the trained fit network.

    python3 -m nn.score --device android-8gb --model Dolphin3.0-Qwen2.5-3B --quant Q8_0
    python3 -m nn.score --device android-8gb --all   # rank every catalog combo

Pre-check only: the score predicts whether the triple fits the
device's budget under pick()'s rule. pick() itself stays the
authority; a high score never installs anything.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_model(weights_path=None):
    path = Path(weights_path) if weights_path else HERE / "weights.json"
    if not path.exists():
        return None
    sys.path.insert(0, str(HERE.parent))
    from nn.mlp import MLP
    data = json.loads(path.read_text(encoding="utf-8"))
    return MLP.from_dict(data["mlp"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Score a (device, model, quant) fit with the NN.")
    ap.add_argument("--device", default=None,
                    help="device name from nn/devices.json")
    ap.add_argument("--model", default=None, help="catalog model name")
    ap.add_argument("--quant", default=None,
                    help="Q4_K_M, Q5_K_M, or Q8_0")
    ap.add_argument("--all", action="store_true",
                    help="rank every catalog combo for the device")
    ap.add_argument("--weights", default=None)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(HERE.parent))
    from nn.data import build_dataset
    from nn.features import extract, top_features

    net = load_model(args.weights)
    if net is None:
        print("no trained weights found. Train first:\n"
              "  python3 -m nn.train", file=sys.stderr)
        return 2
    if not args.device:
        ap.error("give --device (a name from nn/devices.json)")
        return 2
    dataset = build_dataset()
    rows = [r for r in dataset["rows"] if r["device"] == args.device]
    if not rows:
        print(f"unknown device {args.device!r}; known: "
              + ", ".join(sorted({r['device']
                                  for r in dataset['rows']})),
              file=sys.stderr)
        return 2
    if not args.all and (not args.model or not args.quant):
        ap.error("give --model and --quant, or --all")
        return 2
    if not args.all:
        rows = [r for r in rows if r["model"] == args.model
                and r["quant"] == args.quant]
        if not rows:
            print(f"no catalog combo {args.model} {args.quant}",
                  file=sys.stderr)
            return 2
    scored = []
    for r in rows:
        p = net.predict_proba(extract(r))
        scored.append({
            "device": r["device"], "model": r["model"],
            "quant": r["quant"], "size_gb": r["size_gb"],
            "params_b": r["params_b"],
            "nn_score": round(p, 4),
            "nn_label": "fits" if p >= args.threshold else "no-fit",
            "rule_label": "fits" if r["label"] == 1 else "no-fit",
            "is_pick": r["is_pick"],
            "nn_top_features": top_features(r, net),
        })
    scored.sort(key=lambda d: d["nn_score"], reverse=True)
    if args.json:
        print(json.dumps({"device": args.device, "triples": scored},
                         indent=2))
    else:
        print(f"device: {args.device}  (pre-check only; "
              "pick() stays the authority)")
        for s in scored:
            mark = " <-- pick()" if s["is_pick"] else ""
            agree = "" if s["nn_label"] == s["rule_label"] else "  [NN disagrees with rule]"
            print(f"{s['nn_score']:.3f}  {s['nn_label']:<7}  "
                  f"{s['model']} {s['quant']} {s['size_gb']}GB"
                  f"  (rule: {s['rule_label']}){mark}{agree}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
