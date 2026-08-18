"""Configuracao: fail-fast, campos derivados e contrato de require()."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import (
    ConfigError,
    Environment,
    MissingConfigError,
    Settings,
    get_settings,
    reset_settings_cache,
)


def test_sem_secret_key_o_processo_nao_sobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPTMUS_SECRET_KEY", raising=False)
    reset_settings_cache()
    with pytest.raises(ValidationError):
        get_settings()


def test_secret_key_curta_e_rejeitada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_SECRET_KEY", "curta")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="OPTMUS_SECRET_KEY"):
        get_settings()


def test_variavel_vazia_vale_como_ausente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_WEB_BASE_URL", "   ")
    reset_settings_cache()
    assert get_settings().web_base_url is None


def test_db_path_deriva_do_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPTMUS_DB_PATH", raising=False)
    monkeypatch.setenv("OPTMUS_DATA_DIR", str(tmp_path))
    reset_settings_cache()
    assert get_settings().database_path == tmp_path / "optmus.db"


def test_require_lista_o_que_falta(settings: Settings) -> None:
    with pytest.raises(MissingConfigError) as exc:
        settings.require("web_base_url", "web_password", subsystem="optmus_web")
    assert "OPTMUS_WEB_BASE_URL" in str(exc.value)
    assert "OPTMUS_WEB_PASSWORD" in str(exc.value)
    assert exc.value.missing == ["web_base_url", "web_password"]


def test_require_passa_quando_preenchido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_WEB_BASE_URL", "https://optmus.example.com")
    monkeypatch.setenv("OPTMUS_WEB_PASSWORD", "senha-longa-de-cofre")
    reset_settings_cache()
    get_settings().require("web_base_url", "web_password", subsystem="optmus_web")


def test_require_com_campo_inexistente_e_erro_de_programacao(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        settings.require("campo_que_nao_existe", subsystem="x")


def test_settings_e_imutavel(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.http_port = 1234  # type: ignore[misc]


def test_log_json_segue_o_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_ENV", "prod")
    reset_settings_cache()
    prod = get_settings()
    assert prod.env is Environment.PROD
    assert prod.use_json_logs is True
    assert prod.is_dev is False


def test_secret_nao_vaza_no_repr(settings: Settings) -> None:
    assert "chave-de-teste" not in repr(settings)
