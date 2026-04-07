"""Tests for the configuration loader."""

import textwrap
from pathlib import Path

import pytest

from machina.config.loader import load_config, load_yaml
from machina.config.schema import MachinaConfig


class TestLoadYaml:
    """Test YAML loading with env var substitution."""

    def test_load_simple_yaml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "test.yaml"
        cfg.write_text("name: Test Agent\n")
        data = load_yaml(cfg)
        assert data["name"] == "Test Agent"

    def test_env_var_substitution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret-123")
        cfg = tmp_path / "test.yaml"
        cfg.write_text("token: ${MY_TOKEN}\n")
        data = load_yaml(cfg)
        assert data["token"] == "secret-123"

    def test_missing_env_var_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "test.yaml"
        cfg.write_text("token: ${DEFINITELY_NOT_SET}\n")
        with pytest.raises(ValueError, match="DEFINITELY_NOT_SET"):
            load_yaml(cfg)

    def test_nested_env_var_substitution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_PORT", "5432")
        cfg = tmp_path / "test.yaml"
        cfg.write_text(
            textwrap.dedent("""\
            database:
              host: ${DB_HOST}
              port: ${DB_PORT}
            """)
        )
        data = load_yaml(cfg)
        assert data["database"]["host"] == "localhost"
        assert data["database"]["port"] == "5432"

    def test_list_env_var_substitution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test env var substitution inside a YAML list."""
        monkeypatch.setenv("ITEM_A", "alpha")
        monkeypatch.setenv("ITEM_B", "beta")
        cfg = tmp_path / "test.yaml"
        cfg.write_text("items:\n  - ${ITEM_A}\n  - ${ITEM_B}\n  - literal\n")
        data = load_yaml(cfg)
        assert data["items"] == ["alpha", "beta", "literal"]

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_yaml("/nonexistent/path.yaml")


class TestLoadConfig:
    """Test full config loading and validation."""

    def test_load_minimal_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "machina.yaml"
        cfg.write_text("name: My Agent\n")
        config = load_config(cfg)
        assert isinstance(config, MachinaConfig)
        assert config.name == "My Agent"

    def test_load_config_with_connectors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOT_TOKEN", "tok-123")
        cfg = tmp_path / "machina.yaml"
        cfg.write_text(
            textwrap.dedent("""\
            name: Maintenance Bot
            connectors:
              telegram:
                type: telegram
                settings:
                  bot_token: ${BOT_TOKEN}
            llm:
              provider: "ollama:llama3"
              temperature: 0.2
            """)
        )
        config = load_config(cfg)
        assert config.connectors["telegram"].type == "telegram"
        assert config.connectors["telegram"].settings["bot_token"] == "tok-123"
        assert config.llm.provider == "ollama:llama3"
        assert config.llm.temperature == 0.2
