"""Portable shell lookup tests; no external services or credentials."""
import os
from unittest import mock

from bridge import common


def test_termux_prefix_shell_takes_priority():
    with mock.patch.dict(os.environ, {"PREFIX": "/fake/termux"}, clear=False):
        with mock.patch("os.access", side_effect=lambda p, mode: p == "/fake/termux/bin/sh"):
            assert common._shell() == "/fake/termux/bin/sh"


def test_falls_back_to_android_shell():
    with mock.patch.dict(os.environ, {"PREFIX": "/fake/termux"}, clear=False):
        with mock.patch("os.access", side_effect=lambda p, mode: p == "/system/bin/sh"):
            assert common._shell() == "/system/bin/sh"


def test_path_fallback_when_no_fixed_shell_exists():
    with mock.patch.dict(os.environ, {"PREFIX": "/fake/termux"}, clear=False):
        with mock.patch("os.access", return_value=False), mock.patch("shutil.which", return_value="/usr/bin/sh"):
            assert common._shell() == "/usr/bin/sh"
