# nn-100h dataset — linuxtomux

## Task

Given a device and a (model, quant) from `bridge/model_installer.py`
CATALOG, predict whether it **fits the device's budget** under
`pick()`'s rule. Triage-only pre-check: `pick()` stays the
authority, the network never installs anything and never overrides it.

## Provenance (real repo data only, no invented devices, no synthetic rows)

- Built: 2026-11-14 22:39:05 UTC by `nn-100h/build_dataset.py`.
- Models/quants: `bridge/model_installer.py` CATALOG — 4 models, 9 (model, quant) combos with their GGUF sizes.
- Devices (6): the 4 dry-run profiles in `bridge/hwdetect.py` PROFILES, the mesh-test fixture `FAKE_HW` in `tests/test_mesh.py`, and one frozen snapshot of `bridge.hwdetect.detect()` on this VM (`live-vm`, RAM moves so the values are frozen with a timestamp).
- Labels: computed by running the repo's own rule, exactly as `pick()` does: `budget = min(usable_ram_gb / 1.25, storage_free_gb * 0.5)`; label 1 (fits) iff `size_gb <= budget`. `usable_ram_gb` and the storage choice come from the repo's own `bridge.hwdetect.usable_ram_gb` and `bridge.storage.choose_model_storage`. Every device's `pick()` choice is label 1 (asserted at build time).

## Rows

- Total: 54 rows = 6 devices x 9 combos.
- Label balance: 36 fit / 18 no-fit.
- Train: 45 rows (30 fit / 15 no-fit).
- Test: 9 rows (6 fit / 3 no-fit).

## Per-device budgets (pick() rule)

- `android-8gb`: usable_ram 5.2 GB, storage internal free 64.0 GB, budget 4.16 GB, fits 6/9.
- `linux-vm-8gb`: usable_ram 6.5 GB, storage internal free 62.0 GB, budget 5.2 GB, fits 7/9.
- `linux-box-32gb`: usable_ram 29.0 GB, storage internal free 900.0 GB, budget 23.2 GB, fits 9/9.
- `linux-box-64gb`: usable_ram 58.0 GB, storage internal free 1800.0 GB, budget 46.4 GB, fits 9/9.
- `mesh-tiny`: usable_ram 2.0 GB, storage internal free 10.0 GB, budget 1.6 GB, fits 3/9.
- `live-vm`: usable_ram 0.8 GB, storage internal free 38.1 GB, budget 0.64 GB, fits 2/9.

## Features (see train.py FEATURES)

Raw inputs only: `params_b`, `size_gb`, quant one-hots + quant rank,
`usable_ram_gb`, `disk_free_gb` (log-scaled), `is_android`, `cpu_cores`.
Deliberately NOT features: the budget itself and the size/budget ratio.
The network must learn the threshold from the raw numbers.

## Split — GROUPED, leakage audit

- `split.json`, seed 42. Test = the whole `android-8gb` device family
  (the repo's only android-termux device). Train never sees Android.
- Why grouped, not random stratified: `pick()` is a deterministic rule
  in (device, quant). A random split puts near-identical rows on both
  sides, so a high score would mostly mean the network reproduced the
  rule on devices it already saw. The grouped split measures transfer
  to an unseen device family instead.
- Test frac is 9/54 = 0.167, not exactly 0.2: whole-device holdout with
  9-row groups cannot hit 0.2 exactly, and stratification inside a
  single held-out device is impossible by construction.
- **A test accuracy of 1.000 is a leakage signal, not a win.** If it
  happens, investigate (e.g. a feature smuggling the budget) before
  claiming anything. The honest expectation is well below 1.000 on
  9 held-out rows from an unseen platform.
- Note: `is_android` is 0 on every train row and 1 on every test row,
  so the network cannot learn a weight for it from training; transfer
  must come from usable RAM / disk / size. That is part of the test.

## Limits

- 54 rows is small. One test row is 11% of test accuracy; error bars
  are wide and every reported metric must carry its confusion matrix.
- Labels are rule-derived: a high score means the network reproduced
  `pick()`'s threshold, not that it discovered anything about hardware.
- Re-running `build_dataset.py` on another machine re-measures the
  `live-vm` row; that is a new dataset version, commit it as such.
