"""Camada de modelo: a primeira vez que o LEA chama um LLM.

Por que não ``litellm`` ainda
----------------------------
A especificação escolheu ``litellm`` para falar com vários provedores. A
arquitetura aqui preserva essa intenção -- :class:`Summarizer` é um protocolo,
e trocar o backend não mexe em quem chama -- mas o backend que existe hoje usa
o **SDK oficial da Anthropic**, que é o provedor padrão da configuração.

O motivo é prático: saída estruturada com validação (``messages.parse``) e
erros tipados valem mais, agora, que multi-provedor que ninguém está usando.
Os backends Codex CLI e Claude Code do agente próprio são processos locais,
fora desta camada de chamadas por API. Se outro recurso precisar de mais um
provedor HTTP, ele entra atrás deste mesmo protocolo -- possivelmente usando
o próprio ``litellm``.

Funcionar sem credencial é requisito, não cortesia
--------------------------------------------------
Um handoff é o registro do que a sessão deixou pela metade. Se ele só
existisse quando há chave de API configurada, o usuário perderia justamente o
que mais importa no dia em que o modelo está fora do ar ou sem cota. Por isso
:func:`get_summarizer` pode devolver ``None``, e quem chama **tem** que ter um
caminho determinístico -- ver ``lea/handoffs.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Teto de saída. Resumo de handoff é curto; isto é folga, não alvo.
MAX_TOKENS = 4096

#: Prefixo que a configuração usa para dizer o provedor (``anthropic/...``).
_PROVIDER_SEP = "/"

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Não há credencial ou o provedor não está configurado."""


class Summarizer(Protocol):
    """O que o LEA precisa de um modelo nesta fase: texto em, objeto fora."""

    model: str

    async def structured(
        self, *, system: str, prompt: str, schema: type[T]
    ) -> tuple[T, dict[str, int]]:
        """Devolve o objeto validado e o uso de tokens."""
        ...


def split_model(configured: str) -> tuple[str, str]:
    """``anthropic/claude-sonnet-5`` -> ``("anthropic", "claude-sonnet-5")``.

    Sem prefixo, assume Anthropic: é o provedor padrão do produto.
    """
    if _PROVIDER_SEP in configured:
        provider, _, model = configured.partition(_PROVIDER_SEP)
        return provider.strip().lower(), model.strip()
    return "anthropic", configured.strip()


def credential_available(provider: str = "anthropic") -> bool:
    """True se há credencial no ambiente para aquele provedor.

    Só olhamos variáveis de ambiente de propósito. O LEA **não** vasculha o
    perfil de login do Claude Code nem de nenhuma outra ferramenta: aquela
    credencial é daquele programa, e tomá-la emprestada seria usar a conta do
    usuário em chamadas que ele não pediu.
    """
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return False


class AnthropicSummarizer(Summarizer):
    """Backend Anthropic, com saída estruturada validada."""

    def __init__(self, model: str) -> None:
        self.model = model

    async def structured(
        self, *, system: str, prompt: str, schema: type[T]
    ) -> tuple[T, dict[str, int]]:
        import anthropic

        client = anthropic.AsyncAnthropic()
        try:
            response = await client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": schema},
            )
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(f"credencial recusada: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"sem cota agora: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"sem conexão com o provedor: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailable(f"provedor com erro ({exc.status_code})") from exc
            raise
        finally:
            await client.close()

        parsed = response.parsed_output
        if parsed is None:
            raise LLMUnavailable("o modelo não devolveu a estrutura pedida")

        usage = {
            "tokens_in": getattr(response.usage, "input_tokens", 0) or 0,
            "tokens_out": getattr(response.usage, "output_tokens", 0) or 0,
        }
        return parsed, usage


def get_summarizer(configured_model: str) -> Summarizer | None:
    """Backend pronto para uso, ou ``None`` se não dá para chamar modelo.

    ``None`` não é falha: é o sinal de que quem chama deve seguir pelo
    caminho determinístico.
    """
    provider, model = split_model(configured_model)

    if provider != "anthropic":
        logger.info(
            "provedor %r ainda não tem backend; usando o caminho sem LLM", provider
        )
        return None

    if not credential_available(provider):
        logger.info("sem ANTHROPIC_API_KEY; usando o caminho sem LLM")
        return None

    return AnthropicSummarizer(model)


def describe_backend(configured_model: str) -> dict[str, Any]:
    """Estado da integração com o modelo, para o ``doctor`` e para a UI."""
    provider, model = split_model(configured_model)
    has_credential = credential_available(provider)
    return {
        "provider": provider,
        "model": model,
        "available": provider == "anthropic" and has_credential,
        "reason": (
            ""
            if provider == "anthropic" and has_credential
            else (
                f"provedor {provider} ainda sem backend"
                if provider != "anthropic"
                else "ANTHROPIC_API_KEY não definida"
            )
        ),
    }
