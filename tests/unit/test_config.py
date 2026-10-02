"""Tests for the configuration loader."""

import textwrap
from pathlib import Path

import pytest
import yaml

from machina.config.loader import _substitute_env_vars, load_config, load_yaml
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

    def test_utf8_regardless_of_platform_encoding(self, tmp_path: Path) -> None:
        """Non-ASCII values (e.g. an Italian column header) survive loading."""
        cfg = tmp_path / "test.yaml"
        cfg.write_bytes("column: Criticità\nnote: Priorità — alta\n".encode())
        data = load_yaml(cfg)
        assert data["column"] == "Criticità"
        assert data["note"] == "Priorità — alta"


class TestEnvVarDefaults:
    """``${VAR:-default}`` follows POSIX/Docker Compose semantics."""

    @staticmethod
    def _load(tmp_path: Path, text: str) -> dict[str, object]:
        cfg = tmp_path / "test.yaml"
        cfg.write_text(text)
        return load_yaml(cfg)

    def test_default_used_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MACHINA_TEST_MODEL", raising=False)
        data = self._load(tmp_path, 'model: "${MACHINA_TEST_MODEL:-openai/gpt-4o}"\n')
        assert data["model"] == "openai/gpt-4o"

    def test_set_value_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MACHINA_TEST_MODEL", "ollama/llama3")
        data = self._load(tmp_path, 'model: "${MACHINA_TEST_MODEL:-openai/gpt-4o}"\n')
        assert data["model"] == "ollama/llama3"

    def test_default_used_when_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A blank `KEY=` line in an env file counts as unset, as in a shell.
        monkeypatch.setenv("MACHINA_TEST_MODEL", "")
        data = self._load(tmp_path, 'model: "${MACHINA_TEST_MODEL:-openai/gpt-4o}"\n')
        assert data["model"] == "openai/gpt-4o"

    def test_empty_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MACHINA_TEST_KEY", raising=False)
        data = self._load(tmp_path, 'key: "${MACHINA_TEST_KEY:-}"\n')
        assert data["key"] == ""

    def test_defaults_inside_one_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MACHINA_TEST_HOST", raising=False)
        monkeypatch.setenv("MACHINA_TEST_PORT", "9000")
        data = self._load(
            tmp_path,
            'url: "${MACHINA_TEST_SCHEME:-http}://${MACHINA_TEST_HOST:-mock-cmms}:'
            '${MACHINA_TEST_PORT:-8000}/api-v1"\n',
        )
        assert data["url"] == "http://mock-cmms:9000/api-v1"

    def test_missing_without_default_still_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="DEFINITELY_NOT_SET"):
            self._load(tmp_path, "token: ${DEFINITELY_NOT_SET}\n")

    def test_malformed_placeholder_still_raises(self, tmp_path: Path) -> None:
        # A colon without the dash is not the default syntax: it stays an
        # (unset) variable name instead of passing through as literal text.
        with pytest.raises(ValueError, match="NOT_SET:typo"):
            self._load(tmp_path, "token: ${NOT_SET:typo}\n")

    _DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "docker"
    _DEPLOY_VARS = (
        "MACHINA_CMMS_URL",
        "MACHINA_CMMS_API_KEY",
        "MACHINA_SANDBOX_MODE",
        "MACHINA_LOG_LEVEL",
    )

    def test_deploy_docker_config_loads_with_compose_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped Docker config loads with what `docker compose up` passes.

        With an otherwise empty environment, compose resolves the machina
        service's ``${VAR:-default}`` entries to the bundled mock CMMS.
        """
        for name in self._DEPLOY_VARS:
            monkeypatch.delenv(name, raising=False)
        compose = yaml.safe_load(
            (self._DEPLOY_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        )
        for name, value in compose["services"]["machina"]["environment"].items():
            monkeypatch.setenv(name, _substitute_env_vars(str(value)))

        config = load_config(self._DEPLOY_DIR / "config.yaml")

        settings = config.connectors["cmms"].settings
        assert settings["url"] == "http://mock-cmms:9000"
        assert settings["api_key"]
        assert set(settings["endpoints"]) == {
            "get_work_order",
            "update_work_order",
            "read_maintenance_plans",
        }
        assert config.sandbox is True
        assert config.logging["level"] == "INFO"

    def test_deploy_docker_config_requires_the_cmms_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outside compose, a missing CMMS key fails loudly instead of defaulting."""
        for name in self._DEPLOY_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MACHINA_CMMS_URL", "https://cmms.example.com/api")

        with pytest.raises(ValueError, match="MACHINA_CMMS_API_KEY"):
            load_config(self._DEPLOY_DIR / "config.yaml")

    def test_deploy_docker_sandbox_follows_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MACHINA_CMMS_URL", "https://cmms.example.com/api")
        monkeypatch.setenv("MACHINA_CMMS_API_KEY", "k" * 32)
        monkeypatch.setenv("MACHINA_SANDBOX_MODE", "false")

        assert load_config(self._DEPLOY_DIR / "config.yaml").sandbox is False


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

    def test_confirmations_default_true(self) -> None:
        """The ``confirmations`` flag defaults to True (unlike ``sandbox``)."""
        config = MachinaConfig()
        assert config.confirmations is True

    def test_confirmations_false_loads_from_yaml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "machina.yaml"
        cfg.write_text("name: My Agent\nconfirmations: false\n")
        config = load_config(cfg)
        assert config.confirmations is False

    def test_confirmations_reaches_agent_through_from_config(self, tmp_path: Path) -> None:
        """``confirmations: false`` must thread through ``from_config`` to the Agent."""
        from machina.agent.runtime import Agent

        cfg = tmp_path / "machina.yaml"
        cfg.write_text("name: My Agent\nconfirmations: false\n")
        agent = Agent.from_config(cfg)
        assert agent.confirmations is False
