"""Config loader, redaction, and protocol tests."""

import os

import pytest

from cloudops.agent.protocol import extract_fences, fence
from cloudops.common import redact
from cloudops.common.config import HotConfig, _interpolate, config_version, load_yaml


class TestInterpolation:
    def test_env_and_default(self, monkeypatch):
        monkeypatch.setenv("CLOUDOPS_TEST_PORT", "9999")
        data = {"url": "http://x:${CLOUDOPS_TEST_PORT:1234}/mcp",
                "fallback": "${CLOUDOPS_TEST_MISSING:def}", "n": 3}
        out = _interpolate(data)
        assert out["url"] == "http://x:9999/mcp"
        assert out["fallback"] == "def"
        assert out["n"] == 3


class TestHotConfig:
    def test_last_known_good_on_bad_edit(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("value: 1\n")
        cfg = HotConfig(f, validator=lambda d: d)
        assert cfg.value["value"] == 1

        f.write_text("value: [unclosed\n")

        import asyncio

        ok = asyncio.run(cfg.reload())
        assert ok is False
        assert cfg.value["value"] == 1  # last known good survives (FR-CFG-3)

        f.write_text("value: 2\n")
        assert asyncio.run(cfg.reload()) is True
        assert cfg.value["value"] == 2

    def test_last_error_is_recorded_then_cleared(self, tmp_path):
        """FR-CFG-3: a refused edit is retained, not only logged, so the
        console rail can show which file was rejected and why (gap G6)."""
        import asyncio

        f = tmp_path / "battery.yaml"
        f.write_text("value: 1\n")
        cfg = HotConfig(f, validator=lambda d: d)
        assert cfg.last_error is None
        assert cfg.snapshot() == {"file": "battery.yaml", "last_error": None}

        f.write_text("value: [unclosed\n")
        assert asyncio.run(cfg.reload()) is False
        err = cfg.last_error
        assert err is not None
        assert err.file == "battery.yaml"
        assert err.message  # the parser's reason, not a generic string
        assert err.at
        assert cfg.snapshot()["last_error"] == err.as_dict()

        # An unchanged bad file keeps the error standing (no reload churn).
        assert asyncio.run(cfg.reload()) is False
        assert cfg.last_error == err

        f.write_text("value: 2\n")
        assert asyncio.run(cfg.reload()) is True
        assert cfg.last_error is None
        assert cfg.snapshot()["last_error"] is None

    def test_boot_failure_is_loud(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(": : :")
        import yaml

        with pytest.raises(yaml.YAMLError):
            HotConfig(f, validator=lambda d: d)

    def test_config_version_stable(self, tmp_path):
        f = tmp_path / "a.yaml"
        f.write_text("x: 1")
        v1 = config_version([f])
        assert v1 == config_version([f])
        f.write_text("x: 2")
        assert v1 != config_version([f])


class TestRedaction:
    def test_bearer_and_pem(self):
        text = "Authorization: Bearer abcdef123456789 and -----BEGIN PRIVATE KEY-----\nzzz\n-----END PRIVATE KEY-----"
        out = redact.redact_text(text)
        assert "abcdef123456789" not in out
        assert "zzz" not in out
        assert redact.MASK in out

    def test_env_canary_value_scrubbed_anywhere(self, monkeypatch):
        canary = "super-canary-value-1234"
        monkeypatch.setenv("CLOUDOPS_CANARY_SECRET", canary)
        redact.refresh_env_secrets()
        try:
            assert canary not in redact.redact_text(f"leaked: {canary} in a log line")
        finally:
            monkeypatch.delenv("CLOUDOPS_CANARY_SECRET")
            redact.refresh_env_secrets()

    def test_secret_keys_masked_in_objects(self):
        obj = {"password": "hunter2", "nested": {"api_key": "k123456"},
               "fine": "hello", "Authorization": "Bearer tok"}
        out = redact.redact_obj(obj)
        assert out["password"] == redact.MASK
        assert out["nested"]["api_key"] == redact.MASK
        assert out["Authorization"] == redact.MASK
        assert out["fine"] == "hello"

    def test_kv_in_free_text(self):
        out = redact.redact_text("connecting with token=abcd1234efgh")
        assert "abcd1234efgh" not in out


class TestProtocol:
    def test_roundtrip_and_strip(self):
        text = "before\n" + fence("attestation", {"clusters": [1]}) + "\nafter " + fence("phase", {"p": 1})
        narrative, found = extract_fences(text)
        assert narrative == "before\n\nafter"
        assert ("attestation", {"clusters": [1]}) in found
        assert ("phase", {"p": 1}) in found

    def test_malformed_fence_ignored(self):
        narrative, found = extract_fences("```cloudops-x\nnot json\n```rest")
        assert found == []
        assert narrative.endswith("rest")


class TestSettingsPrecedence:
    def test_yaml_loads(self, tmp_path):
        f = tmp_path / "m.yaml"
        f.write_text("a: ${HOME}/x\n")
        assert load_yaml(f)["a"] == os.environ["HOME"] + "/x"
