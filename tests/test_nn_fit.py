"""Tests for the nn/ fit predictor: dataset honesty + training smoke test."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nn.data import build_dataset, stratified_split  # noqa: E402
from nn.features import extract, feature_names  # noqa: E402
from nn.mlp import MLP, metrics_for  # noqa: E402


def test_dataset_is_54_real_rows_with_both_classes():
    dataset = build_dataset()
    rows = dataset["rows"]
    assert len(rows) == 54, dataset["stats"]
    assert dataset["stats"]["devices"] == 6
    assert dataset["stats"]["combos_per_device"] == 9
    assert dataset["stats"]["n_positive"] == 37
    assert dataset["stats"]["n_negative"] == 17
    # Every device's pick() choice is labeled as fitting.
    assert dataset["stats"]["picks_all_fit"] is True
    assert sum(1 for r in rows if r["is_pick"]) == 6


def test_features_are_raw_inputs_only():
    names = feature_names()
    assert len(names) == 10
    assert not any("budget" in n or "ratio" in n for n in names)
    dataset = build_dataset()
    for row in dataset["rows"]:
        vec = extract(row)
        assert len(vec) == 10
        assert all(0.0 <= v <= 1.0 or v == 0.0 for v in vec)


def test_tiny_train_runs_and_scores():
    dataset = build_dataset()
    train_rows, test_rows = stratified_split(dataset["rows"], 0.2, 42)
    X_train = [extract(r) for r in train_rows]
    y_train = [r["label"] for r in train_rows]
    X_test = [extract(r) for r in test_rows]
    y_test = [r["label"] for r in test_rows]
    net = MLP([10, 8, 1], seed=42)
    net.fit(X_train, y_train, epochs=5, batch_size=16)
    scores = [net.predict_proba(x) for x in X_test]
    m = metrics_for(y_test, scores)
    assert 0.0 <= m["roc_auc"] <= 1.0
    assert m["n"] == len(test_rows)
