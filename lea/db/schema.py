"""Esquema do banco, como uma lista ordenada de migrações.

Decisão de projeto: o esquema inteiro mora **neste arquivo**. A referência que
estudamos (Hermes Agent) espalhou o estado por mais de setenta módulos
``hermes_state_*.py`` na raiz do pacote, e o custo disso é que ninguém mais
consegue responder "quais tabelas existem?" sem um grep. Enquanto couber numa
leitura, fica junto.

Regras das migrações:

* Migração é **imutável depois de publicada**. Corrigir algo = acrescentar uma
  nova, nunca editar uma que já rodou na máquina de alguém.
* Cada uma é uma lista de comandos aplicados numa transação só.
* O índice da lista é a versão (1-based), guardada no ``PRAGMA user_version``.
"""

from __future__ import annotations

#: Tokenizador do FTS. ``remove_diacritics 2`` faz "memoria" achar "memória",
#: que importa num produto cuja interface e cujo conteúdo são em português.
FTS_TOKENIZER = "unicode61 remove_diacritics 2"


_INITIAL: tuple[str, ...] = (
    # ── Projetos ─────────────────────────────────────────────────────────
    """
    CREATE TABLE projects (
        id                     INTEGER PRIMARY KEY,
        slug                   TEXT    NOT NULL UNIQUE,
        name                   TEXT    NOT NULL,
        path                   TEXT    NOT NULL,
        remote                 TEXT,
        default_branch         TEXT,
        language               TEXT,
        created_at             TEXT    NOT NULL,
        -- O lea-agent só roda sozinho aqui se isto for 1. Padrão desligado:
        -- autonomia é opt-in por projeto, não por instalação.
        allow_autonomous       INTEGER NOT NULL DEFAULT 0,
        budget_brl_per_session REAL,
        max_duration_min       INTEGER,
        require_approval_for   TEXT NOT NULL DEFAULT '[]'
    )
    """,
    # ── Memória ──────────────────────────────────────────────────────────
    # O corpo da página NÃO vive aqui: a fonte da verdade é o arquivo .md no
    # repositório git da wiki. Esta tabela é índice, e pode ser reconstruída
    # do zero a partir do git -- é o que `lea doctor --reindex` fará.
    """
    CREATE TABLE pages (
        id               TEXT    PRIMARY KEY,
        path             TEXT    NOT NULL UNIQUE,
        project_id       INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        title            TEXT    NOT NULL,
        tier             TEXT    NOT NULL DEFAULT 'warm'
                                 CHECK (tier IN ('hot', 'warm', 'cold')),
        strength         REAL    NOT NULL DEFAULT 0.5
                                 CHECK (strength >= 0.0 AND strength <= 1.0),
        last_access_at   TEXT,
        access_count_7d  INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT    NOT NULL,
        sha              TEXT
    )
    """,
    "CREATE INDEX idx_pages_project ON pages(project_id)",
    "CREATE INDEX idx_pages_tier_strength ON pages(tier, strength DESC)",
    f"""
    CREATE VIRTUAL TABLE pages_fts USING fts5(
        title,
        body,
        tags,
        page_id UNINDEXED,
        tokenize = '{FTS_TOKENIZER}'
    )
    """,
    # dst_id fica nulo enquanto o destino do [[wikilink]] não existir. Link
    # quebrado é estado legítimo: o agente cita uma página que ainda vai ser
    # escrita, e a consolidação resolve depois.
    """
    CREATE TABLE page_links (
        src_id   TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        dst_slug TEXT NOT NULL,
        dst_id   TEXT REFERENCES pages(id) ON DELETE SET NULL,
        PRIMARY KEY (src_id, dst_slug)
    )
    """,
    "CREATE INDEX idx_page_links_dst ON page_links(dst_id)",
    """
    CREATE TABLE page_access (
        id         INTEGER PRIMARY KEY,
        page_id    TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        agent      TEXT NOT NULL,
        session_id TEXT,
        at         TEXT NOT NULL,
        kind       TEXT NOT NULL
                   CHECK (kind IN ('read', 'cite', 'write', 'handoff_cite'))
    )
    """,
    "CREATE INDEX idx_page_access_page_at ON page_access(page_id, at DESC)",
    # ── Sessões e eventos ────────────────────────────────────────────────
    """
    CREATE TABLE sessions (
        id                TEXT    PRIMARY KEY,
        project_id        INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        harness           TEXT    NOT NULL,
        agent_label       TEXT,
        status            TEXT    NOT NULL DEFAULT 'starting'
                                  CHECK (status IN ('starting', 'active', 'paused',
                                                    'ended', 'handoff', 'error')),
        mode              TEXT    NOT NULL DEFAULT 'interactive'
                                  CHECK (mode IN ('interactive', 'autonomous', 'handoff')),
        branch            TEXT,
        worktree_path     TEXT,
        task              TEXT,
        started_at        TEXT    NOT NULL,
        ended_at          TEXT,
        tokens_in         INTEGER NOT NULL DEFAULT 0,
        tokens_out        INTEGER NOT NULL DEFAULT 0,
        cost_brl          REAL    NOT NULL DEFAULT 0.0,
        pid               INTEGER,
        parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_sessions_status ON sessions(status, started_at DESC)",
    "CREATE INDEX idx_sessions_project ON sessions(project_id, started_at DESC)",
    "CREATE INDEX idx_sessions_parent ON sessions(parent_session_id)",
    """
    CREATE TABLE events (
        id           INTEGER PRIMARY KEY,
        session_id   TEXT REFERENCES sessions(id) ON DELETE CASCADE,
        at           TEXT NOT NULL,
        type         TEXT NOT NULL
                     CHECK (type IN ('prompt', 'tool_call', 'tool_result', 'message',
                                     'skill', 'memory', 'cron', 'alert',
                                     'session_start', 'session_end')),
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_events_session_at ON events(session_id, at DESC)",
    "CREATE INDEX idx_events_at ON events(at DESC)",
    # ── Handoffs ─────────────────────────────────────────────────────────
    """
    CREATE TABLE handoffs (
        id                    TEXT    PRIMARY KEY,
        from_session_id       TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        project_id            INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        title                 TEXT    NOT NULL,
        stopped_at_md         TEXT    NOT NULL DEFAULT '',
        failed_md             TEXT    NOT NULL DEFAULT '',
        open_items_json       TEXT    NOT NULL DEFAULT '[]',
        cited_pages_json      TEXT    NOT NULL DEFAULT '[]',
        suggested_harness     TEXT,
        status                TEXT    NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending', 'claimed', 'done')),
        claimed_by_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        created_at            TEXT    NOT NULL,
        claimed_at            TEXT
    )
    """,
    "CREATE INDEX idx_handoffs_status ON handoffs(status, created_at DESC)",
    # ── Skills ───────────────────────────────────────────────────────────
    # O formato em disco é SKILL.md com frontmatter compatível com Agent
    # Skills (o mesmo do Claude Code e do Hermes), para a skill ser portável
    # entre as ferramentas. O que é só do LEA vai sob `metadata.lea`.
    """
    CREATE TABLE skills (
        id          INTEGER PRIMARY KEY,
        name        TEXT    NOT NULL UNIQUE,
        version     TEXT    NOT NULL,
        origin      TEXT    NOT NULL DEFAULT 'manual'
                            CHECK (origin IN ('auto', 'manual')),
        description TEXT    NOT NULL DEFAULT '',
        triggers    TEXT    NOT NULL DEFAULT '[]',
        path        TEXT    NOT NULL,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE skill_versions (
        id         INTEGER PRIMARY KEY,
        skill_id   INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
        version    TEXT    NOT NULL,
        diff       TEXT    NOT NULL DEFAULT '',
        reason     TEXT    NOT NULL DEFAULT '',
        created_by TEXT,
        created_at TEXT    NOT NULL,
        -- Proposta pendente quando auto_apply_skill_updates está desligado.
        applied    INTEGER NOT NULL DEFAULT 1,
        UNIQUE (skill_id, version)
    )
    """,
    """
    CREATE TABLE skill_runs (
        id          INTEGER PRIMARY KEY,
        skill_id    INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
        version     TEXT    NOT NULL,
        session_id  TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        success     INTEGER NOT NULL,
        duration_ms INTEGER,
        at          TEXT    NOT NULL
    )
    """,
    "CREATE INDEX idx_skill_runs_skill_at ON skill_runs(skill_id, at DESC)",
    # ── Automações ───────────────────────────────────────────────────────
    """
    CREATE TABLE cron_jobs (
        id              INTEGER PRIMARY KEY,
        instruction_nl  TEXT    NOT NULL,
        cron_expr       TEXT    NOT NULL,
        timezone        TEXT    NOT NULL DEFAULT 'America/Sao_Paulo',
        channel         TEXT,
        project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        skill_id        INTEGER REFERENCES skills(id) ON DELETE SET NULL,
        status          TEXT    NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'paused')),
        next_run_at     TEXT,
        created_at      TEXT    NOT NULL
    )
    """,
    "CREATE INDEX idx_cron_jobs_next ON cron_jobs(status, next_run_at)",
    """
    CREATE TABLE cron_runs (
        id         INTEGER PRIMARY KEY,
        job_id     INTEGER NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
        started_at TEXT    NOT NULL,
        ended_at   TEXT,
        status     TEXT    NOT NULL DEFAULT 'ok'
                           CHECK (status IN ('ok', 'alert', 'error')),
        summary    TEXT    NOT NULL DEFAULT '',
        log_json   TEXT    NOT NULL DEFAULT '[]',
        cost_brl   REAL    NOT NULL DEFAULT 0.0
    )
    """,
    "CREATE INDEX idx_cron_runs_job ON cron_runs(job_id, started_at DESC)",
    # ── Canais ───────────────────────────────────────────────────────────
    # Sem segredo aqui: token de bot vai para o keyring do sistema, e
    # config_json guarda só o que não é sensível (ids de canal, allowlist).
    """
    CREATE TABLE channels (
        id           INTEGER PRIMARY KEY,
        platform     TEXT    NOT NULL,
        display      TEXT    NOT NULL,
        config_json  TEXT    NOT NULL DEFAULT '{}',
        status       TEXT    NOT NULL DEFAULT 'disconnected',
        last_error   TEXT,
        connected_at TEXT,
        UNIQUE (platform, display)
    )
    """,
    # ── Custos ───────────────────────────────────────────────────────────
    """
    CREATE TABLE costs (
        id         INTEGER PRIMARY KEY,
        session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
        harness    TEXT    NOT NULL,
        model      TEXT    NOT NULL,
        tokens_in  INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        cost_usd   REAL    NOT NULL DEFAULT 0.0,
        cost_brl   REAL    NOT NULL DEFAULT 0.0,
        at         TEXT    NOT NULL
    )
    """,
    "CREATE INDEX idx_costs_at ON costs(at DESC)",
    "CREATE INDEX idx_costs_session ON costs(session_id)",
)


#: Migrações em ordem. O índice+1 é a versão gravada em ``PRAGMA user_version``.
_CLI_AGENT_BACKENDS: tuple[str, ...] = (
    "ALTER TABLE cron_jobs ADD COLUMN agent_backend TEXT NOT NULL DEFAULT 'codex' "
    "CHECK (agent_backend IN ('codex', 'claude-code', 'anthropic-api'))",
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_INITIAL, _CLI_AGENT_BACKENDS)

#: Versão alvo do esquema.
LATEST_VERSION = len(MIGRATIONS)
