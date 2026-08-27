"""Configuracao tipada do Optmus Core.

Regras:
- Toda configuracao vem do ambiente (.env). Nenhum segredo em codigo.
- O processo NAO sobe com configuracao obrigatoria faltando (fail-fast).
- Credenciais de fases futuras sao opcionais aqui, mas cada subsistema
  chama ``settings.require(...)`` na sua inicializacao. Assim o erro
  aparece com nome de variavel, e nao como ``NoneType`` tres camadas abaixo.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEY_MIN_LENGTH: Final[int] = 32
# Ultima minor do Python com wheels para faster-whisper / openwakeword / mediapipe.
MAX_SUPPORTED_PYTHON_MINOR: Final[int] = 12


class ConfigError(RuntimeError):
    """Configuracao invalida ou incompleta."""


class MissingConfigError(ConfigError):
    """Variaveis de ambiente obrigatorias para um subsistema estao ausentes."""

    def __init__(self, subsystem: str, missing: list[str]) -> None:
        self.subsystem = subsystem
        self.missing = missing
        nomes = ", ".join(f"OPTMUS_{n.upper()}" for n in missing)
        super().__init__(f"{subsystem}: configuracao ausente no .env -> {nomes}")


class Environment(StrEnum):
    DEV = "dev"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Settings(BaseSettings):
    """Configuracao completa do Core. Instancie via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="OPTMUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---------------------------------------------------------------- nucleo
    env: Environment = Environment.DEV

    secret_key: SecretStr = Field(
        ...,
        description=(
            "Chave mestra: deriva segredos e criptografa tokens OAuth em repouso. "
            'Gere com: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        ),
    )

    data_dir: Path = Path("data")
    db_path: Path | None = None

    log_level: LogLevel = LogLevel.INFO
    log_json: bool | None = None

    # O mapa das bases do Notion, como JSON, direto do ambiente.
    #
    # Existe porque o mapa e configuracao que IDENTIFICA RECURSO PESSOAL: ele
    # carrega os database_id das bases do usuario, e este repositorio e
    # publico. Nao pode ir para a imagem nem para o git.
    #
    # SecretStr para nao aparecer em repr, log ou traceback. Nao e credencial -
    # ler exige OPTMUS_NOTION_TOKEN -, mas e identificador estavel de dado
    # pessoal, e nada ganha em ser impresso.
    notion_map_json: SecretStr | None = None

    # Origens que podem chamar a API de dentro de um navegador. Lista explicita,
    # nunca "*": o navegador so faz valer a regra de mesma origem se o servidor
    # disser quem pode. O padrao cobre o `vite dev`; o dominio da Vercel entra
    # por OPTMUS_CORS_ORIGINS no painel, na hora do deploy do frontend.
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:5174"]
    )

    http_host: str = "127.0.0.1"
    # PORT sem prefixo porque Railway, Render e Fly injetam essa variavel.
    http_port: int = Field(
        default=8420,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("OPTMUS_HTTP_PORT", "PORT"),
    )
    # Obrigatorio quando o Core escuta fora de 127.0.0.1 - ver security/api_auth.py
    api_token: SecretStr | None = None

    bus_queue_maxsize: int = Field(default=1000, ge=1)
    event_retention_days: int = Field(default=90, ge=0)

    # ------------------------------------------------------------ identidade
    user_name: str = "Luiz"
    user_honorific: str = "senhor"
    wake_word: str = "optmus"

    # --------------------------------------------------------------- memoria
    embedding_dim: int = Field(default=1024, ge=8, le=8192)
    embedding_provider: str = Field(default="auto", pattern="^(auto|hashing|fastembed)$")
    embedding_model: str = "intfloat/multilingual-e5-small"

    working_memory_ttl_min: int = Field(default=30, ge=1)
    working_memory_turns: int = Field(default=12, ge=1)

    # Meia-vida do decaimento: episodio envelhece rapido, fato semantico nao.
    episodic_half_life_days: float = Field(default=14.0, gt=0)
    semantic_half_life_days: float = Field(default=180.0, gt=0)

    recall_limit: int = Field(default=5, ge=0, le=50)
    recall_min_score: float = Field(default=0.12, ge=0.0, le=1.0)

    # Rotina exige repeticao em semanas distintas: 3 pedidos na mesma sexta
    # sao uma tarefa, nao um habito.
    procedural_min_occurrences: int = Field(default=3, ge=2)

    consolidator_enabled: bool = True
    consolidator_hour: int = Field(default=4, ge=0, le=23)

    profile_path: Path | None = None

    # --------------------------------------------------------------- cerebro
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"
    # Nao desligamos o thinking: com ele desligado o modelo as vezes escreve a
    # chamada de ferramenta como TEXTO e a ferramenta nunca roda - falha
    # silenciosa inaceitavel para um sistema que opera dispositivos.
    # effort baixo da quase a mesma latencia sem essa classe de bug.
    llm_effort: str = Field(default="low", pattern="^(low|medium|high|xhigh|max)$")
    llm_max_tokens: int = Field(default=1024, ge=64)
    llm_max_rounds: int = Field(default=6, ge=1, le=20)
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"

    # ------------------------------------------------------------- stt / tts
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    whisper_language: str = "pt"
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_flash_v2_5"
    piper_model_path: Path | None = None
    piper_binary: str = "piper"

    # --------------------------------------------------------------- visao
    # O opt-in de verdade e instalar o extra [visao]: sem OpenCV a ferramenta
    # nem entra no schema do modelo. Esta flag e o interruptor para desligar a
    # camera sem desinstalar nada - util quando a maquina esta em reuniao, ou
    # emprestada.
    vision_enabled: bool = True
    camera_index: int = Field(default=0, ge=0, le=8)

    # ----------------------------------------------------------------- voz
    voice_enabled: bool = False
    audio_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    audio_frame_ms: int = Field(default=20, ge=10, le=60)
    audio_input_device: str | None = None
    audio_output_device: str | None = None

    wake_model_path: Path | None = None
    wake_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    wake_cooldown_s: float = Field(default=1.5, ge=0.0)

    # auto = webrtcvad quando instalado, energia quando nao. Fixe em "energia"
    # se o webrtcvad estiver cortando fala no seu microfone.
    vad_backend: str = Field(default="auto", pattern="^(auto|energia|webrtcvad)$")
    vad_silence_ms: int = Field(default=700, ge=100)
    vad_energy_threshold: int = Field(default=400, ge=0)
    vad_max_utterance_s: float = Field(default=15.0, gt=0)
    vad_min_utterance_ms: int = Field(default=250, ge=0)

    # Meta da secao 3: wake word -> primeira silaba. Acima disso, log de alerta.
    latency_target_ms: int = Field(default=1200, ge=1)
    metrics_window: int = Field(default=200, ge=10)

    # ------------------------------------------------------------ optmus web
    web_base_url: str | None = None
    web_password: SecretStr | None = None
    web_timeout_s: float = Field(default=5.0, gt=0)
    # Contrato conferido contra a API real: Authorization: Bearer <token>,
    # token = HMAC-SHA256(chave='jarvis-auth-v1', mensagem=senha).
    # "header" existe so como escape se o Web passar a ler um header proprio.
    web_auth_scheme: str = Field(default="bearer", pattern="^(bearer|header)$")
    web_auth_header: str = "X-Optmus-Auth"

    # ---------------------------------------------------------- busca web
    # Ferramenta server-side da Anthropic: sem chave extra, com citacao.
    # So funciona com o cerebro na nuvem - busca web offline nao existe.
    web_search_enabled: bool = True
    web_search_max_uses: int = Field(default=5, ge=1, le=20)

    # --------------------------------------------------------------- notion
    # Acesso direto a fonte, para conferir os numeros contra o Optmus Web antes
    # de qualquer decisao sobre desligar o Web.
    notion_token: SecretStr | None = None
    notion_version: str = "2022-06-28"
    notion_timeout_s: float = Field(default=15.0, gt=0)
    notion_map_path: Path | None = None
    notion_months_window: int = Field(default=6, ge=1, le=36)
    notion_weeks_window: int = Field(default=8, ge=1, le=52)
    notion_workout_weeks: int = Field(default=6, ge=1, le=52)
    # Meses anteriores na media do /api/stats/forecast. 3 foi MEDIDO.
    notion_forecast_history_months: int = Field(default=3, ge=1, le=24)
    notion_alert_window_days: int = Field(default=30, ge=1, le=365)
    # Limite para TRAS nos alertas. None = sem limite, que e o comportamento
    # atual e NAO foi conferido contra o Web: um painel de prazos dificilmente
    # mostra uma prova de 8 meses atras. Preencha quando a medicao disser qual e.
    notion_alert_past_days: int | None = Field(default=None, ge=0, le=3650)

    # ----------------------------------------------------------- integracoes
    telegram_bot_token: SecretStr | None = None
    # Destino UNICO das mensagens, e de proposito.
    #
    # Se o chat fosse parametro da ferramenta, uma instrucao vinda de conteudo
    # que o Optmus le - um e-mail, uma pagina, uma linha do Notion - poderia
    # redirecionar mensagens para terceiros. Com o destino na configuracao,
    # esse ataque deixa de existir: o modelo escolhe o texto, nunca para quem.
    telegram_chat_id: str | None = None

    # WhatsApp NAO OFICIAL (neonize/whatsmeow). Local e numero secundario -
    # ver docs/WHATSAPP.md. O caminho oficial (Business API) foi descartado:
    # janela de 24 h e templates aprovados pela Meta, sem as palavras do Optmus.
    whatsapp_enabled: bool = False
    whatsapp_session_path: Path = Path("data/whatsapp.db")
    # Apelido -> numero. Fora do controle de versao: numeros de gente real.
    whatsapp_contacts_path: Path = Path("data/contatos.json")

    whatsapp_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    instagram_token: SecretStr | None = None
    instagram_account_id: str | None = None
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    homeassistant_base_url: str | None = None
    homeassistant_token: SecretStr | None = None

    # -------------------------------------------------------------- seguranca
    external_action_rate_limit: int = Field(default=20, ge=0)
    destructive_passphrase: SecretStr | None = None
    destructive_delay_s: float = Field(default=5.0, ge=0)
    tool_sandbox_runs: int = Field(default=3, ge=0)
    # ------------------------------------------------------------- F10
    # Modo desenvolvedor. Desligado por padrao e so local.
    dev_enabled: bool = False
    dev_projects_path: Path = Path("data/projetos.json")
    # Teto de arquivos apagados num commit. Acima disso o commit e recusado -
    # freio estrutural contra "apaga tudo", que nao depende de o codigo gerado
    # ser bom.
    dev_max_delecoes: int = Field(default=8, ge=0)
    # Ferramentas EXTERNAS cuja confirmacao humana foi DISPENSADA por decisao
    # explicita do usuario (26/08/2026: "eu controlo a frequencia, nao existe
    # trava tecnica no caminho de deploy").
    #
    # Mora aqui, e nao num atributo da ferramenta, para a dispensa ser UMA
    # lista visivel em vez de uma flag espalhada. Ha teste garantindo que
    # camera, WhatsApp, Telegram e Instagram nunca entram nela.
    dev_sem_portao: tuple[str, ...] = ("dev_publicar",)

    # -------------------------------------------------------------- F8
    # Abrir arquivo e app de verdade. Desligado por padrao, e so local:
    # `tools/impl/pc.py` recusa em plataforma hospedada sem consultar isto.
    pc_enabled: bool = False
    # Apps e pastas registrados a mao. Fora do controle de versao - sao
    # caminhos da sua maquina. Formato em docs/HUD_GESTUAL.md.
    pc_targets_path: Path = Path("data/alvos.json")

    # -------------------------------------------------------------- F7
    # Desligada por padrao. Proatividade e a unica funcao do Optmus que fala
    # sem ser chamada; ligar sozinha seria decidir por quem instalou.
    proactive_enabled: bool = False
    # Teto rigido de avisos por dia civil. Zero desliga tao bem quanto a flag.
    proactive_daily_budget: int = Field(default=5, ge=0)
    proactive_interval_min: int = Field(default=30, ge=1)
    # Janela de silencio [inicio, fim). Aviso que venceria aqui e DESCARTADO,
    # nao adiado - ver o modulo: a fonte e relida a cada ciclo, entao o que
    # ainda importar de manha aparece sozinho.
    proactive_quiet_start: int = Field(default=22, ge=0, le=23)
    proactive_quiet_end: int = Field(default=8, ge=0, le=23)
    # Mesmo assunto nao se repete dentro disso.
    proactive_cooldown_h: int = Field(default=12, ge=1)

    # -------------------------------------------------------------- validacao
    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: Any) -> Any:
        """Variavel presente e vazia no .env vale como ausente, nao como "" ."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("secret_key")
    @classmethod
    def _secret_key_forte(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < SECRET_KEY_MIN_LENGTH:
            raise ValueError(
                f"OPTMUS_SECRET_KEY precisa de ao menos {SECRET_KEY_MIN_LENGTH} caracteres"
            )
        return value

    # ----------------------------------------------------------- derivados
    @property
    def database_path(self) -> Path:
        """Caminho efetivo do SQLite."""
        return self.db_path if self.db_path is not None else self.data_dir / "optmus.db"

    @property
    def notion_map_file(self) -> Path:
        """Caminho efetivo do mapa de bases do Notion."""
        if self.notion_map_path is not None:
            return self.notion_map_path
        return self.data_dir / "notion_map.json"

    @property
    def profile_file(self) -> Path:
        """Caminho efetivo do ``perfil.md``."""
        return self.profile_path if self.profile_path is not None else self.data_dir / "perfil.md"

    @property
    def is_dev(self) -> bool:
        return self.env is Environment.DEV

    @property
    def use_json_logs(self) -> bool:
        if self.log_json is not None:
            return self.log_json
        return self.env is Environment.PROD

    # ------------------------------------------------------------- contratos
    def require(self, *names: str, subsystem: str) -> None:
        """Falha se algum campo listado estiver vazio.

        Chamado pelos subsistemas no startup, nao no import: a F0 sobe sem
        chave da Anthropic, e o loop de voz da F1 recusa subir sem ela.
        """
        desconhecidos = [n for n in names if n not in type(self).model_fields]
        if desconhecidos:
            raise ConfigError(f"campos inexistentes em Settings: {desconhecidos}")

        faltando = [n for n in names if _vazio(getattr(self, n))]
        if faltando:
            raise MissingConfigError(subsystem, faltando)

    def missing(self, *names: str) -> list[str]:
        """Mesma checagem de :meth:`require`, sem levantar. Usado no /health."""
        return [n for n in names if _vazio(getattr(self, n, None))]


def _vazio(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, SecretStr):
        return not value.get_secret_value().strip()
    if isinstance(value, str):
        return not value.strip()
    return False


def runtime_notes() -> list[str]:
    """Avisos de ambiente que nao impedem a F0 mas quebram fases seguintes."""
    notes: list[str] = []
    if sys.version_info[:2] > (3, MAX_SUPPORTED_PYTHON_MINOR):
        atual = f"{sys.version_info.major}.{sys.version_info.minor}"
        notes.append(
            f"Python {atual}: faster-whisper, openwakeword e mediapipe (F1/F8) nao "
            f"publicam wheels acima de 3.{MAX_SUPPORTED_PYTHON_MINOR}. "
            "Crie o venv com Python 3.12 antes da F1."
        )
    return notes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Configuracao do processo, lida uma vez e cacheada."""
    return Settings()  # type: ignore[call-arg]  # campos vem do ambiente


def reset_settings_cache() -> None:
    """Limpa o cache de :func:`get_settings` (testes)."""
    get_settings.cache_clear()
