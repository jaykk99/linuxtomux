# linuxtomux NN — model-fit predictor

Small pure-Python-stdlib neural network (tanh hidden, sigmoid out, SGD),
in the style of the ReconAnything NN scorer. Zero dependencies.

## Prediction task

Given a device and a (model, quant) from `bridge/model_installer.py`
CATALOG, predict whether it **fits the device's budget** under
`pick()`'s rule: `size_gb <= min(usable_ram / 1.25, disk_free * 0.5)`.

## Dataset (real repo data only)

- 6 devices from `nn/devices.json`: the 4 dry-run profiles in
  `bridge/hwdetect.py` PROFILES, the mesh-test fixture `FAKE_HW` in
  `tests/test_mesh.py`, and one frozen snapshot of
  `bridge.hwdetect.detect()` on this VM.
- 9 catalog combos (4 models x their quants).
- 54 rows total: 37 fit / 17 no-fit. Stratified 80/20 split
  (44 train / 10 test). Every device's `pick()` choice is label 1;
  training asserts that.

## Honest limits

- **Labels are rule-derived.** Each row is labeled by running the
  repo's own `pick()` budget rule on the real catalog and device
  records. A high score means the network reproduced that threshold,
  not that it discovered anything. 100% test accuracy is the
  rule-reproduction flag, not a win.
- **Small data.** 54 rows, 10 in the test set: one test row is 10%
  of test accuracy. Judge by ROC AUC and confusion counts.
- **Pre-check only.** The model never installs anything and never
  overrides `pick()`; `pick()` stays the authority.
- Features are raw inputs only (params, size, quant, usable RAM, free
  disk, platform, cores). The budget and the size/budget ratio are
  deliberately NOT features, so the network must learn the threshold.

## Use

```
python3 -m nn.train            # retrain, refresh viewer-data.json
python3 -m nn.score --device android-8gb --all
python3 -m nn.score --device linux-vm-8gb --model Dolphin3.0-Llama3.1-8B --quant Q4_K_M
```

Open `nn/viewer.html` as `file://` for the metrics.
