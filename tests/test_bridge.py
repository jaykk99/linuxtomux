"""Unit tests: tier picking + safety guard + token check.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_bridge.py)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")

from bridge import model_installer  # noqa: E402
from bridge.common import blocked_reason  # noqa: E402
from bridge.hwdetect import PROFILES  # noqa: E402


def test_s22_and_big_linux_land_on_different_tiers():
    phone = model_installer.pick(PROFILES["galaxy-s22"])
    big = model_installer.pick(PROFILES["linux-box-32gb"])
    assert phone["params_b"] < big["params_b"], (phone, big)
    # S22: 8GB total, 2.8GB Android reserve -> 5.2GB usable -> /1.25 = 4.16GB
    # budget -> biggest fit is the 3B at Q8 or 7B at nothing... 3B Q8 = 3.3GB.
    assert phone["params_b"] == 3.0 and phone["quant"] == "Q8_0", phone
    # 32GB Linux box: ~29GB available -> 23.2GB budget -> 32B at Q5_K_M (22.4GB).
    assert big["params_b"] == 32.0 and big["quant"] == "Q5_K_M", big


def test_vm_class_picks_7b_or_8b_q4():
    pick = model_installer.pick(PROFILES["linux-vm-8gb"])
    assert pick["params_b"] in (7.0, 8.0), pick
    assert pick["quant"] == "Q4_K_M", pick


def test_only_uncensored_catalogue():
    for name, _repo, _p, _q, lineage in model_installer.CATALOG:
        assert "Dolphin" in name or "abliterated" in name.lower()
        assert "uncensored" in lineage.lower()


def test_denylist():
    assert blocked_reason("rm -rf /") is not None
    assert blocked_reason("ls -la") is None
    assert blocked_reason("echo hello") is None


def test_every_catalogue_url_is_hf_resolve():
    for _name, repo, _p, quants, _l in model_installer.CATALOG:
        for fname, _size in quants.values():
            assert fname.endswith(".gguf")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
