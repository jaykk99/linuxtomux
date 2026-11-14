# nn-100h — 100-hour neural network trainer (linuxtomux)

Predicts whether a (device, model, quant) from
`bridge/model_installer.py` CATALOG fits the device budget under
`pick()`'s rule. Pure Python stdlib, self-contained MLP (tanh hidden,
sigmoid output, SGD). Triage-only: `pick()` stays the authority, this
never installs anything and never overrides it.

## Files (all in this directory — this is the local run home too)

| File | What it is |
|---|---|
| `train.py` | The trainer. `python3 train.py [--resume] [--max-run-hours H]` (default 5.5). |
| `build_dataset.py` | Builds `dataset.jsonl`, `split.json`, `DATASET.md` from the repo's own CATALOG + device profiles. |
| `dataset.jsonl` | 54 rows: 6 real devices x 9 catalog combos, labels from the repo's own `pick()` rule. |
| `split.json` | FROZEN grouped split, seed 42: test = whole `android-8gb` family (9 rows), train = other 5 devices (45 rows). Never train on test rows. |
| `DATASET.md` | Provenance, per-device budgets, leakage audit. Read it before quoting any metric. |
| `experiments.jsonl` | One line per finished experiment: `{exp_id, config, test_acc, auc, confusion, per_class_recall, seconds}` (+ train_acc/train_loss). |
| `checkpoints/<exp_id>.json` | Every 20 epochs while an experiment runs; deleted when it completes. |
| `best.json` / `best-metrics.json` | Best experiment by honest test accuracy (AUC tiebreak). `best.json` holds the weights. |
| `status.json` | `{repo, hours_trained (cumulative), experiments_done, best, status: training\|done, updated}`. |
| `heartbeat.json` | `{pid, time, exp_id, epoch, experiments_done}`, at least every 30 s. |

## Run it

```sh
cd nn-100h
python3 train.py --max-run-hours 5.5   # one chunk; exits 0 on time budget
python3 train.py --resume              # after a kill / reboot: continue the
                                        # in-progress experiment from its checkpoint
```

Chunks: the VM reboots roughly every 2 h, so run in chunks well under
that (default 5.5 h is for a stable host; on this VM use ~1.5 h).
Hours accumulate in `status.json` across restarts. At 100 cumulative
hours `status` becomes `done` and the trainer exits 0.

```sh
# resume after a reboot, in a loop until done (example)
while true; do
  python3 train.py --resume --max-run-hours 1.5
  python3 -c "import json; raise SystemExit(0 if json.load(open('status.json'))['status']=='done' else 1)"
done
```

## Grid

Deterministic: experiment `n` is `config_for_index(n)` — hidden in
{4, 8, 16, 32}, lr in {0.01, 0.03, 0.1, 0.3}, L2 in {0, 1e-4, 1e-3},
epochs in {100, 300, 600}, seeds unbounded (144 configs per seed), so
the grid cannot run out before 100 h. Batch size 16, Xavier init,
per-epoch shuffle seeded by (seed, epoch) so a resumed run reproduces
the uninterrupted epoch order exactly.

## Honest-metrics rules

- Test is the grouped holdout (unseen Android family). Report test
  accuracy with its confusion matrix and per-class recall, never alone.
- **Test accuracy 1.000 = leakage signal, not a win.** Investigate
  (a feature smuggling the budget) before claiming anything.
- One test row is 11% of test accuracy; error bars are wide.
- Labels are rule-derived: a high score means the network reproduced
  `pick()`'s threshold, not that it discovered anything.
