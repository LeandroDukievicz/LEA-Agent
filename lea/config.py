"""Configuração do LEA, lida de ``~/.lea/config.toml``.

Precedência: variável de ambiente ``LEA_*`` > ``config.toml`` > padrão do
código. O arquivo é escrito com comentários na primeira execução, porque ele é
feito para ser editado à mão.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from lea import paths


class ServerConfig(BaseModel):
    """Onde o daemon escuta.

    O padrão é 127.0.0.1 e mudar isso expõe o LEA à rede -- com ele vão as
    ferramentas do agente, que executam comandos. A UI avisa quando o host sai
    do loopback; ver ``is_exposed``.
    """

    host: str = "127.0.0.1"
    port: int = Field(default=7420, ge=1, le=65535)
    keep_running_in_tray: bool = True

    @property
    def is_exposed(self) -> bool:
        """True se o daemon aceita conexão de fora da máquina."""
        return self.host not in ("127.0.0.1", "::1", "localhost")


class HalfLifeDays(BaseModel):
    """Meia-vida do decaimento de força, por tier, em dias."""

    hot: float = Field(default=3, gt=0)
    warm: float = Field(default=21, gt=0)
    cold: float = Field(default=120, gt=0)


class MemoryConfig(BaseModel):
    wiki_path: Path = Field(default_factory=paths.wiki_dir)
    inject_budget_tokens: int = Field(default=8000, gt=0)
    half_life_days: HalfLifeDays = Field(default_factory=HalfLifeDays)
    consolidate_cron: str = "0 23 * * *"
    backup_remote: str = ""

    #: Revisão em background depois de cada turno: o agente é forkado e
    #: pergunta "algo aqui merece virar memória ou skill?". Herda o runtime do
    #: pai, então acerta o mesmo prompt cache e sai barato. Complementa a
    #: consolidação noturna, não substitui -- quem aplica decaimento e promove
    #: tier continua sendo ela.
    background_review: bool = True

    @field_validator("wiki_path", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        return Path(value).expanduser() if isinstance(value, str) else value


class AgentConfig(BaseModel):
    default_model: str = "anthropic/claude-sonnet-5"
    default_backend: Literal["codex", "claude-code", "anthropic-api"] = "codex"
    auto_apply_skill_updates: bool = False
    max_duration_min: int = Field(default=30, ge=1, le=1440)
    max_parallel_tasks: int = Field(default=2, ge=1, le=16)
    max_tool_rounds: int = Field(default=12, ge=1, le=100)
    max_tokens_per_task: int = Field(default=24000, ge=1000, le=1_000_000)
    command_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    require_approval_for: list[str] = Field(
        default_factory=lambda: ["git push", "rm -rf", "sudo", "curl", "wget"]
    )


class CostsConfig(BaseModel):
    daily_budget_brl: float = Field(default=40.0, ge=0)
    usd_brl: float = Field(default=5.40, gt=0)


class UIConfig(BaseModel):
    scanlines: bool = True
    language: str = "pt-BR"


class Config(BaseSettings):
    """Configuração completa. Use ``load()`` em vez de instanciar direto."""

    model_config = SettingsConfigDict(
        env_prefix="LEA_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Ambiente vence o arquivo, que vence o padrão."""
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, paths.config_file()),
        )


def load() -> Config:
    """Lê a configuração, criando o arquivo com os padrões se ele não existir."""
    path = paths.config_file()
    if not path.exists():
        write_default(path)
    return Config()


def write_default(path: Path) -> Config:
    """Escreve um ``config.toml`` com os valores padrão e volta o objeto."""
    config = Config()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    body = tomli_w.dumps(payload)
    path.write_text(_HEADER + body, encoding="utf-8")
    return config


_HEADER = """\
# Configuração do LEA. Editar à mão é o uso esperado.
#
# Precedência: variável de ambiente LEA_* > este arquivo > padrão do código.
# Aninhado usa duplo sublinhado: LEA_SERVER__PORT=7421
#
# Atenção: server.host fora de 127.0.0.1 expõe o LEA à rede, e junto vão as
# ferramentas do agente, que rodam comandos na sua máquina.

"""
