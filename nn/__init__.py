"""Fit-prediction network for linuxtomux (pure Python stdlib).

Predicts whether a (model, quant) from bridge/model_installer.py
CATALOG fits a device's budget under pick()'s rule. Triage /
pre-check only: it never installs anything and never overrides
pick(), which stays the authority.

Layout (same shape as the ReconAnything NN scorer):
- mlp.py: tanh hidden, sigmoid out, SGD, pure stdlib (reusable as-is)
- features.py: (device, model, quant) -> 10-number vector (raw inputs
  only; the budget and size/budget ratio are NOT features)
- data.py: builds the 54 labeled rows (6 devices x 9 catalog combos)
  from nn/devices.json + the real CATALOG, labels from pick()'s rule
- train.py: trains, writes weights.json + viewer-data.json
- score.py: scores one triple or ranks all combos for a device
- viewer.html: metrics viewer (open as file://)
- devices.json: the 6 device records + provenance

Train: python3 -m nn.train
Score: python3 -m nn.score --device android-8gb --all
"""
