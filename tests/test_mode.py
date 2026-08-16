from __future__ import annotations

from pathlib import Path

import pytest

from papertrader.accounts import DEFAULT_DATA_DIR, DEFAULT_LIVE_DATA_DIR
from papertrader.mode import ModeError, load_dotenv_file, normalize_mode, resolve_mode


def test_load_dotenv_file_does_not_override(tmp_path):
    path = tmp_path / ".env"
    path.write_text("FOO=fromfile\nBAR=kept\n")
    env = {"FOO": "existing"}
    load_dotenv_file(path, env)
    assert env["FOO"] == "existing"
    assert env["BAR"] == "kept"
    assert normalize_mode("test") == "paper"
    assert normalize_mode("paper") == "paper"
    assert normalize_mode("LIVE") == "live"
    with pytest.raises(ModeError):
        normalize_mode("demo")


def test_resolve_defaults_to_paper(tmp_path):
    resolved = resolve_mode(
        settings_mode="paper",
        cli_mode=None,
        confirm_live=False,
        data_dir=tmp_path,
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=1,
        env={},
    )
    assert not resolved.is_live
    assert resolved.data_dir == tmp_path


def test_yaml_live_without_confirm_is_rejected():
    with pytest.raises(ModeError, match="confirm"):
        resolve_mode(
            settings_mode="live",
            cli_mode=None,
            confirm_live=False,
            data_dir=DEFAULT_DATA_DIR,
            clob_host="https://clob.polymarket.com",
            chain_id=137,
            signature_type=1,
            env={"PAPERTRADER_PRIVATE_KEY": "0xabc"},
        )


def test_live_requires_private_key():
    with pytest.raises(ModeError, match="PRIVATE_KEY"):
        resolve_mode(
            settings_mode="paper",
            cli_mode="live",
            confirm_live=True,
            data_dir=DEFAULT_DATA_DIR,
            clob_host="https://clob.polymarket.com",
            chain_id=137,
            signature_type=1,
            env={},
        )


def test_live_uses_isolated_data_dir():
    resolved = resolve_mode(
        settings_mode="paper",
        cli_mode="live",
        confirm_live=True,
        data_dir=DEFAULT_DATA_DIR,
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=1,
        env={"PAPERTRADER_PRIVATE_KEY": "0xabc", "PAPERTRADER_LIVE": "1"},
    )
    assert resolved.is_live
    assert resolved.data_dir == DEFAULT_LIVE_DATA_DIR


def test_explicit_data_dir_kept_in_live(tmp_path: Path):
    resolved = resolve_mode(
        settings_mode="live",
        cli_mode=None,
        confirm_live=True,
        data_dir=tmp_path,
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=1,
        env={"PAPERTRADER_PRIVATE_KEY": "0xabc"},
    )
    assert resolved.data_dir == tmp_path


def test_cli_paper_overrides_yaml_live():
    resolved = resolve_mode(
        settings_mode="live",
        cli_mode="test",
        confirm_live=False,
        data_dir=DEFAULT_DATA_DIR,
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=1,
        env={"PAPERTRADER_PRIVATE_KEY": "0xabc", "PAPERTRADER_LIVE": "1"},
    )
    assert resolved.mode == "paper"
