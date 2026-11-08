"""Cliente do ai-memory: o que o backend anthropic-api usa como memória."""

import pytest

from lea import aimemory


def test_ferramentas_so_aparecem_com_token(monkeypatch, tmp_path):
    """Oferecer uma ferramenta que sempre falha ensina o modelo a abandoná-la."""
    monkeypatch.delenv("AI_MEMORY_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(aimemory, "TOKEN_FILE", tmp_path / "nao-existe")
    assert aimemory.configured() is False

    monkeypatch.setenv("AI_MEMORY_AUTH_TOKEN", "t0k3n")
    assert aimemory.configured() is True


def test_token_do_ambiente_vence_o_do_arquivo(monkeypatch, tmp_path):
    arquivo = tmp_path / "auth-token"
    arquivo.write_text("do-arquivo\n")
    monkeypatch.setattr(aimemory, "TOKEN_FILE", arquivo)
    monkeypatch.delenv("AI_MEMORY_AUTH_TOKEN", raising=False)
    assert aimemory.auth_token() == "do-arquivo"

    monkeypatch.setenv("AI_MEMORY_AUTH_TOKEN", "do-ambiente")
    assert aimemory.auth_token() == "do-ambiente"


def test_resposta_em_sse_e_lida_igual():
    """O transporte pode responder evento a evento; pedimos os dois formatos."""
    bruto = 'event: message\ndata: {"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"ok"}]}}\n'
    assert aimemory._text_of(aimemory._decode(bruto)) == "ok"


def test_erro_do_servidor_vira_excecao_legivel():
    bruto = '{"jsonrpc":"2.0","error":{"code":-32602,"message":"path obrigatório"}}'
    with pytest.raises(aimemory.MemoryUnavailable, match="path obrigatório"):
        aimemory._decode(bruto)


async def test_memoria_fora_do_ar_nao_derruba_a_tarefa(monkeypatch):
    """Vira texto para o agente: ele fica sabendo que a memória caiu, em vez de
    concluir que não há nada gravado."""
    async def cai(*args, **kwargs):
        raise aimemory.MemoryUnavailable("ai-memory inalcançável: conexão recusada")

    monkeypatch.setattr(aimemory, "call", cai)
    saida = await aimemory.dispatch("recall", {"query": "qualquer coisa"})
    assert saida.startswith("Error:")
    assert "inalcançável" in saida


async def test_argumentos_vazios_sao_recusados_antes_da_rede():
    assert (await aimemory.dispatch("recall", {"query": "  "})).startswith("Error:")
    assert (await aimemory.dispatch("remember", {"path": "a", "body": ""})).startswith("Error:")
