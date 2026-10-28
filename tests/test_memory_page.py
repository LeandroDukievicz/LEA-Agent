"""Formato da página: frontmatter, wikilinks e ida e volta."""

from __future__ import annotations

import pytest

from lea.memory import page as page_mod
from lea.memory.page import Page, PageError, normalize_slug, parse

EXEMPLO = """\
---
id: 0x7a3f19
title: Decaimento de força da memória
tier: warm
strength: 0.71
tags: [memoria, arquitetura]
created_by: codex
updated_by: claude-code
---

A força cai com o tempo. Ver [[lea-core/tiers]] e [[preferencias]].
"""


def test_parse_le_todos_os_campos() -> None:
    p = parse(EXEMPLO, path="lea-core/memoria/decaimento.md")
    assert p.id == "0x7a3f19"
    assert p.title == "Decaimento de força da memória"
    assert p.tier == "warm"
    assert p.strength == pytest.approx(0.71)
    assert p.tags == ["memoria", "arquitetura"]
    assert p.created_by == "codex"
    assert p.updated_by == "claude-code"
    assert p.body.startswith("A força cai")


def test_slug_projeto_e_qualificado() -> None:
    p = parse(EXEMPLO, path="lea-core/memoria/decaimento.md")
    assert p.slug == "decaimento"
    assert p.project == "lea-core"
    assert p.qualified_slug == "lea-core/decaimento"


def test_pagina_global_nao_tem_projeto() -> None:
    p = parse(EXEMPLO, path="_global/preferencias.md")
    assert p.project is None
    assert p.qualified_slug == "preferencias"


def test_links_extraidos_sem_repeticao_e_em_ordem() -> None:
    p = parse(EXEMPLO, path="lea-core/memoria/decaimento.md")
    assert p.links() == ["lea-core/tiers", "preferencias"]


def test_links_normalizam_caixa_e_acento() -> None:
    p = Page(id="0x1", title="T", body="[[Memória/Decaimento]] e [[memoria/decaimento]]")
    assert p.links() == ["memoria/decaimento"]


def test_ida_e_volta_preserva_conteudo() -> None:
    original = parse(EXEMPLO, path="lea-core/memoria/decaimento.md")
    reparsed = parse(original.to_markdown(), path=original.path)
    assert reparsed.id == original.id
    assert reparsed.title == original.title
    assert reparsed.tags == original.tags
    assert reparsed.body == original.body
    assert reparsed.tier == original.tier


def test_acento_no_titulo_nao_vira_escape() -> None:
    """O arquivo é para ser lido e editado por humano."""
    p = Page(id="0x1", title="Decaimento de força", body="corpo")
    assert "Decaimento de força" in p.to_markdown()
    assert "\\u" not in p.to_markdown()


@pytest.mark.parametrize(
    ("texto", "erro"),
    [
        ("sem frontmatter nenhum", "frontmatter"),
        ("---\nid: 0x0a0b0c\n", "não fechado"),
        ("---\ntitle: X\n---\n", "sem `id`"),
        ("---\nid: 0x0a0b0c\n---\n", "sem `title`"),
        ("---\nid: 0x0a0b0c\ntitle: X\ntier: morna\n---\n", "tier inválido"),
        ("---\nid: 0x0a0b0c\ntitle: X\nstrength: 5\n---\n", "fora de 0..1"),
        ("---\nid: 0x0a0b0c\ntitle: X\nstrength: abc\n---\n", "não numérico"),
        ("---\n- lista\n---\n", "mapa"),
    ],
)
def test_frontmatter_invalido_tem_mensagem_util(texto: str, erro: str) -> None:
    with pytest.raises(PageError, match=erro):
        parse(texto)


def test_tags_como_string_e_tolerado() -> None:
    """Erro comum de quem edita à mão."""
    p = parse("---\nid: '0x0a0b0c'\ntitle: X\ntags: 'a, b'\n---\n")
    assert p.tags == ["a", "b"]


def test_bom_do_windows_nao_quebra() -> None:
    p = parse("﻿---\nid: '0x0a0b0c'\ntitle: X\n---\ncorpo")
    assert p.id == "0x0a0b0c"


def test_id_sem_aspas_e_lido_como_hexadecimal_pelo_yaml() -> None:
    """Armadilha real: em YAML 1.1, `0x7a3f19` é o inteiro 8011545.

    O LEA sempre grava o id entre aspas, mas um arquivo editado à mão pode
    cair nisso -- então aceitamos o inteiro e voltamos à forma canônica.
    """
    p = parse("---\nid: 0x7a3f19\ntitle: X\n---\n")
    assert p.id == "0x7a3f19"


def test_id_com_zero_a_esquerda_sobrevive_a_ida_e_volta() -> None:
    """token_hex gera ~6% de ids com zero à esquerda; perdê-lo trocaria a
    identidade da página e deixaria o índice órfão."""
    original = Page(id="0x07a3f1", title="X", body="corpo", path="p/x.md")
    assert parse(original.to_markdown(), path=original.path).id == "0x07a3f1"


def test_id_e_sempre_gravado_entre_aspas() -> None:
    assert "'0x7a3f19'" in Page(id="0x7a3f19", title="X").to_markdown()


def test_yaml_nao_executa_construtor_python() -> None:
    """safe_load: o corpo pode ter vindo de entrada não confiável."""
    perigoso = "---\nid: '0x0a0b0c'\ntitle: !!python/object/apply:os.system ['echo oi']\n---\n"
    with pytest.raises(PageError):
        parse(perigoso)


def test_ids_gerados_sao_distintos_e_no_formato() -> None:
    ids = {page_mod.new_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("0x") and len(i) == 8 for i in ids)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Memória/Decaimento", "memoria/decaimento"),
        ("decaimento.md", "decaimento"),
        ("  Força  ", "forca"),
        ("a_b c", "a-b-c"),
    ],
)
def test_normalize_slug(entrada: str, esperado: str) -> None:
    assert normalize_slug(entrada) == esperado
