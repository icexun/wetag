# WeTag

English | [中文](README.md)

A collaboration assistant running on a **WeCom (WeChat Work) AI bot**. @ it in a group chat to ask questions, create to-dos, read and write docs and sheets, manage requirements, and subscribe to requirement/document change notifications. External systems are reached through their official CLIs — `wecom-cli`, `lark-cli` (Feishu/Lark) and `tapd-cli` (TAPD) — each optional except WeCom.

## Product design

### A group-collaboration agent

A personal agent serves one person; WeTag serves **multi-role collaboration inside a group** — PMs, engineers, QA and stakeholders moving work forward in one shared context.

| | Personal agent | WeTag |
|---|---|---|
| Serves | One user | Every role in the group |
| Context | Personal session | Group → topic, shared across people |
| Writes | User's own responsibility | Risk-tiered approval, visible and audited in the group |
| Knowledge | Personal memory | Layered group / workspace / org memory and rules |
| Trigger | Reactive Q&A | Q&A + proactive change notifications |

![Product architecture](docs/images/product-architecture.svg)

(Diagrams are labeled in Chinese.)

### Typical flow

```mermaid
sequenceDiagram
    participant P as PM
    participant W as WeTag
    participant T as TAPD
    participant F as Feishu doc
    participant D as Dev/QA
    P->>W: @WeTag create a story from this PRD
    W->>T: Create story (low-risk write: run, then report)
    W-->>P: Reply with card link
    P->>W: @WeTag add acceptance criteria to the PRD
    W-->>P: High-risk write: approval card in group
    P->>W: Approve (or an Auto Mode rule passes it)
    W->>F: Update doc
    loop Scheduled polling
        W->>F: Check doc changes
        W->>T: Check story field changes
    end
    W-->>D: Push changes to the group
```

### Highlights

- **Group-collaboration agent** — the group is the unit of work; messages are routed into topics so everyone shares the same context.
- **Topic routing** — quoted message → TAPD ID / Feishu link → semantic match → new topic. Prefer a new topic over a wrong merge.
- **Hard workspace isolation** — four spaces: org / workspace / group / topic. Workspaces have separate processes, DB schemas and roles, and credentials.
- **Approval gate + Auto Mode** — writes are tiered in four levels; high-risk writes need approval first. Auto Mode rules can pass selected operations without approval, still audited.
- **Layered memory and rules** — group / workspace / org memory; rules override from org → workspace → group → topic.
- **Feedback loop** — 👎 feedback is periodically distilled into workspace reply rules, effective on the next message. Guardrails: a vote threshold, caps on count and length, a dedicated namespace that never overwrites human rules, and a change log plus group notice for every update.
- **CLI first, behind ports** — external systems run only through whitelisted official CLIs; business code depends only on `contracts/ports.py`; integrations are pluggable.

| Risk level | Examples | Handling |
|---|---|---|
| Read | Read docs, query stories, search code | Run directly |
| Low-risk write | Create to-do, event, story | Run, then report |
| High-risk write | Send message/mail, write shared sheet, edit others' docs | Run after in-group approval |
| Delete | Any delete | Not exposed; rejected |

| Memory layer | Scope | How it's written |
|---|---|---|
| Group | Facts and conventions of this group | Written in conversation; low-confidence items await review |
| Workspace | All groups in the workspace | Same as above |
| Org | All workspaces | Proposal only, needs human confirmation |

## Features

| Integration | Capabilities |
|---|---|
| WeCom (required) | Group-chat Q&A over the bot long connection, to-dos, contacts, messages, calendar, online meetings, docs, sheets, smart sheets, drive, mail |
| Feishu / Lark (optional) | Docs read/create/edit/search, sheets, Base (bitable), tasks, calendar, approval lookup, wiki; doc change notifications |
| TAPD (optional) | Stories / bugs / tasks query & edit, comments, iterations, members, change history; requirement change notifications |
| Local repo (optional) | Read-only code search |

Platform:

- **Approval gate** — writes are risk-tiered; high-risk writes (sending messages/mail, writing shared sheets…) post an approval card in the group and run only after approval. Delete operations are not exposed.
- **Workspace isolation** — one process, one PostgreSQL schema and one dedicated DB role per workspace; only that workspace's credentials are injected.
- **Reliable delivery** — every entry point becomes a persisted Task; outbound goes through an outbox; failures are explicit, never silent.
- **Memory & rules** — group/workspace memory; 👎 feedback is distilled into reply rules.

## Quick start (Docker)

1. Create an **AI bot** (API mode, long connection) in the WeCom admin console; note the Bot ID and Secret.
2. Configure:

   ```bash
   cp .env.example .env
   # required: API_KEY, BASE_URL, MODEL_NAME (any OpenAI-compatible endpoint),
   #           WECOM_BOT_ID, WECOM_BOT_SECRET, WS_DB_PASSWORD
   ```

3. Start PostgreSQL + migrations + WeTag:

   ```bash
   docker compose up -d
   docker compose logs -f wetag   # startup log lists each integration's status
   ```

4. Authorize the CLIs (login state persists in the `cli-home` volume):

   ```bash
   docker compose exec -it wetag wecom-cli auth init   # WeCom
   docker compose exec -it wetag lark-cli auth login   # Feishu (optional)
   ```

Add the bot to a group and @ it. WeCom only? Set `INTEGRATIONS=wecom` in `.env`.

## Local development

Requires Python ≥ 3.11, PostgreSQL ≥ 14 and the CLIs you enable.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

PG_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/wetag \
  python scripts/init_workspace.py --apply-org --workspace demo --password <WS_DB_PASSWORD>

python -m app.bootstrap   # production entry: long connection + workers + scheduler + HTTP(:8080)
python main.py            # agent-only debug server: OpenAI-compatible HTTP(:9000)

PG_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/wetag pytest
pyflakes app tests
```

## Configuration

Precedence: process env > `.env` > `config/workspaces/<WORKSPACE_ID>.yaml` > `config/wetag.env` > code defaults. All settings live in [`config/properties.py`](config/properties.py).

| Variable | Meaning |
|---|---|
| `WORKSPACE_ID` | Workspace bound to this process |
| `API_KEY` / `BASE_URL` / `MODEL_NAME` | LLM (OpenAI-compatible) |
| `WECOM_BOT_ID` / `WECOM_BOT_SECRET` | WeCom AI bot |
| `PG_DSN` | Connection string of the workspace role |
| `INTEGRATIONS` | Enabled integrations, default `tapd,lark,wecom` |
| `LARK_CLI_TOKEN` | Feishu (optional; or `lark-cli auth login`) |
| `TAPD_TOKEN` / `TAPD_API_ENDPOINT` | TAPD personal token and API endpoint |

Per-workspace settings (TAPD project, Feishu domain, repo path…) go in `config/workspaces/<id>.yaml`, using [`demo.yaml`](config/workspaces/demo.yaml) as a template; other files there are git-ignored.

A disabled integration, or one whose CLI is not on `PATH`, has its port marked unavailable, its tools and skills unregistered and its polling jobs unscheduled; the startup log says why.

## Architecture

![Technical architecture](docs/images/tech-architecture.svg)

Dependencies flow `gateway → space → capability → control`; `runtime` is cross-cutting, `analytics` read-only; `contracts` / `platform` never import business modules.

| Choice | Why |
|---|---|
| WeCom AI bot long connection | No public callback URL; deployable inside an intranet |
| One process per workspace | Failures, credentials and data are isolated; scale out per workspace |
| Every entry point becomes a persisted Task | Messages, schedules and card callbacks share one retryable, auditable queue |
| Deterministic jobs separate from the agent | Polling and sync don't call the LLM: predictable cost and results |
| CLI first, business code depends on ports | Reuse official CLI auth and features; swap implementations without touching business code |

![Workspace isolation](docs/images/isolation.svg)

See [`docs/design/`](docs/design/overall.md) (Chinese).

## Contributing & security

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)

## License

[Apache-2.0](LICENSE)
