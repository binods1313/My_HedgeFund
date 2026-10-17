from __future__ import annotations

import pytest

from engine import __main__ as engine_main


def test_check_config_exits_before_acquiring_the_engine_lock(capsys, monkeypatch):
    def fail_if_locked(_config):
        pytest.fail("--check-config must not acquire the running engine lock")

    monkeypatch.setattr(engine_main, "_acquire_single_instance_lock", fail_if_locked)

    assert engine_main.main(["--check-config"]) == 0
    assert "[config] OK" in capsys.readouterr().out
