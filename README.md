# Flowise Questionnaire System

A Django app for analyzing and simulating [Colectica](https://www.colectica.com/)-format survey
questionnaire JSON files (e.g. Understanding Society Mainstage waves). It lets designers upload a
questionnaire module, extract its question schema and routing logic, build a visual flow graph,
run an AI-assisted advisory review via [Flowise](https://flowiseai.com/), and interactively
simulate walking through the questionnaire as a respondent.

It can also ingest a Forsta+ (Confirmit Horizons) XML export of the same questionnaire wave and
structurally compare its routing against the Colectica-derived routing, surfacing any
discrepancies — missing branches, unmatched conditions, Forsta+-only "else" branches — in a
dedicated routing-diff GUI, with a side-by-side graph view per discrepancy.

## Core architectural principle

**Django owns all data, routing logic, and validation. Flowise is advisory only.**

Flowise (an external LLM agent platform) is used for two narrow purposes, and its output is
always validated/post-processed by Django before being trusted:

1. **Module Review agentflow** — reviews routing/coverage for design issues. Django sends a
   compact payload and post-validates the response against known facts, rejecting anything that
   invents question names or modifies routing.
2. **Interview Wording agentflow** — reformats respondent-facing question text/options during the
   interview simulator. Django validates the response and falls back to a deterministic,
   locally-built message if Flowise is unavailable or returns something invalid — the simulator
   always works even if Flowise is down.

## Processing pipeline

Enforced in the UI/views as a strict order per uploaded module:

1. **Upload** JSON → `QuestionnaireModule`
2. **Extract schema** (`schema_extractor.ColecticaSchemaExtractor`) → `NormalizedQuestion` rows
3. **Extract routing** (`routing_extractor.ColecticaRoutingExtractor`) → `RoutingEdge` rows
   (conditional / sequential / loop)
4. **Build graph** (`graph_builder.py` + `graph_enrichment.py`) → `QuestionnaireGraph`
   (nodes/edges JSON + Mermaid text)
5. **Run Flowise review** (optional) → `ModuleAIReview`

Everything runs synchronously in the request/response cycle — there is no Celery/async task
queue.

### Colectica vs Forsta+ routing diff

A second, independent pipeline runs against a second `QuestionnaireModule`
(`source_format` auto-detected as `forsta_xml` from the `.xml` extension at upload — same upload
form as Colectica), cross-validating a fieldwork agency's Forsta+ (Confirmit Horizons) XML export
against the Colectica-derived routing for the same wave:

1. **Upload** Forsta+ XML → `QuestionnaireModule`
2. **Extract schema** (`forsta_xml_schema_extractor.ForstaXmlSchemaExtractor`) → `NormalizedQuestion` rows
3. **Extract routing** (`forsta_xml_routing_extractor.ForstaXmlRoutingExtractor`) → `RoutingEdge` rows
4. **Match questions** (`question_matcher.build_question_matches`) → `QuestionMatch` rows, pairing
   each Colectica question with its best Forsta+ counterpart in three passes: exact
   normalized-text match, then fuzzy fallback (`difflib`, 0.75 threshold), then a **name-tiebreak**
   reconciliation step — if a question's current match has a different name than itself, and an
   unused same-named question exists on the other side whose own wording *also* clears the fuzzy
   threshold (or is a clean prefix/substring match — Forsta+ source text sometimes folds
   interviewer instructions inline where Colectica keeps them separate), the same-named one takes
   over. Name is a tiebreak, never an override: a same-named-but-unrelated-content "false friend"
   is left alone.
5. **Compare routing** (`routing_comparator.compare_routing_for_modules`) → `RoutingDiscrepancy`
   rows — a *structural* diff (edge target presence only, not condition semantics). Both the
   source and target question of each edge are resolved through `QuestionMatch` before comparing,
   not compared as raw name strings, so a target present on both sides under a different name
   (casing, a Forsta+ suffix, etc.) isn't wrongly reported as missing.

Browsable at `/questionnaires/routing-diff/<colectica_module_id>/<forsta_module_id>/`, with a
per-discrepancy detail page rendering both systems' routing graphs side by side.

## MCP tool server

Alongside the main Django app, `mcp_server` exposes a curated, read-only subset of the same data
as [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) tools over streamable-HTTP,
for MCP clients like Claude Desktop — its own Django app, its own standalone process, its own
port, never the main `runserver`.

```bash
python manage.py runmcp                 # own process, port 8765 by default
```

Every request must carry a per-person access token in its URL path
(`https://<host>/t/<token>/mcp`) rather than a header, since MCP client connector UIs generally
only take a URL. Staff users generate/revoke tokens at `/questionnaires/mcp-tokens/` — no shared
secret, no terminal command needed to onboard a new person, and revoking one person's token
doesn't affect anyone else's.

**Requesting access:** there's no self-serve signup by design. Open an issue on this repository
or contact the maintainer to request a token.

Tools (`mcp_server/tools.py`), each with a matching MCP **prompt** (`mcp_server/prompts.py`)
that MCP clients can surface as a slash-command-style shortcut:

| Tool | Prompt | Notes |
|---|---|---|
| `list_modules` | `listModules` | Colectica-only |
| `get_module_summary` | `showModuleSummary` | Colectica-only |
| `list_questions` | `listQuestions` | Colectica-only |
| `get_question` | `showQuestion` | Colectica-only |
| `get_routing_edges` | `listRoutingEdges` | Colectica-only |
| `trace_variable` | `traceVariable` | Colectica-only |
| `get_module_graph` | `showModuleGraph` | Colectica + Forsta+; also returns Mermaid flowchart syntax so an MCP client can render an actual diagram; large modules are auto-summarized instead of returning a huge graph |
| `evaluate_edge_condition` | `evaluateCondition` | Colectica + Forsta+; evaluate any condition string against hypothetical answers |
| `get_routing_diff_report` | `showRoutingDiffReport` | Colectica + Forsta+ pair; mirrors the routing-diff report page |
| `get_routing_discrepancy_detail` | `showColecticaForstaDiscrepancy` | Colectica + Forsta+ pair; mirrors the per-discrepancy detail page, both systems' graphs included |
| `get_routing_simulation` | `showRoutingSimulation` | Colectica-only |

Never writes to the database and never triggers a compute-heavy pipeline step (extraction, graph
building, matching/comparison, AI review) itself — every tool reads data some other part of the
app already computed and persisted.

## Tech stack

- Django 6.0 (`config/` project; two apps, `flowise_questionnaire` and `mcp_server`)
- PostgreSQL (`flowise_questionnaire_db`)
- Flowise (external, self-hosted or cloud) for advisory AI review/wording
- No frontend framework — server-rendered Django templates (routing graphs rendered client-side
  via [vis-network](https://visjs.github.io/vis-network/), loaded from a CDN)

## Getting started

### Prerequisites

- Python 3.12+
- PostgreSQL, with a `flowise_questionnaire_db` database available
- A running Flowise instance (optional — only needed for the AI review / interview wording
  features; the rest of the app works without it)

### Setup

```bash
git clone https://github.com/amiravarzamani/colectica-forsta-routing-toolkit.git
cd flowise-questionnaire-system

python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `SECRET_KEY`/`DB_PASSWORD`/`DB_HOST` — `config/settings.py`
has no defaults for these and will fail loudly at startup if they're missing. Adjust the
`DATABASES` and `FLOWISE_*` settings in `config/settings.py` to match your environment before
running migrations.

```bash
python manage.py migrate
python manage.py createsuperuser   # first user, since login is required app-wide
python manage.py runserver
```

The app is mounted at `/questionnaires/` and requires login (`LOGIN_URL = /questionnaires/login/`).

### Running tests

```bash
python manage.py test
```

## Project structure

```
config/                         Django project settings, URLs, WSGI/ASGI
flowise_questionnaire/
  models.py                     QuestionnaireModule, NormalizedQuestion, RoutingEdge,
                                 QuestionnaireGraph, ModuleAIReview, InterviewSimulatorSession/Turn,
                                 QuestionMatch, RoutingDiscrepancy
  services/                     Pipeline logic, in order:
    schema_extractor.py           parse questions out of the Colectica JSON
    routing_extractor.py          parse conditional/sequential/loop routing (Colectica)
    forsta_xml_schema_extractor.py   parse questions out of the Forsta+ XML
    forsta_xml_routing_extractor.py  parse conditional/sequential/loop routing (Forsta+)
    graph_builder.py               build the routing graph
    graph_enrichment.py            annotate the graph
    condition_evaluator.py         evaluate Colectica-syntax routing conditions against answers
    forsta_condition_evaluator.py  evaluate Forsta+-syntax routing conditions against answers
    coverage_intent_builder.py     generate deterministic test-case seed inputs
    routing_simulator.py           check routing coverage
    question_matcher.py            pair Colectica and Forsta+ questions (exact + fuzzy + name-tiebreak)
    routing_comparator.py          structural diff of matched questions' routing edges (source + target resolved via QuestionMatch)
    routing_diff_explainer.py      plain-language explanation text for the routing-diff GUI
    agentflow_payload_builder.py   build the Module Review Flowise payload
    flowise_client.py              send/receive the Module Review agentflow
    interview_router.py            deterministic routing engine for the simulator
    interview_simulator_service.py orchestrate simulator sessions
    answer_validation.py           validate respondent A/B/C input
    question_presentation.py       convert questions to respondent-facing text
    flowise_interview_wording.py   Interview Wording agentflow client + caching/fallback
    interview_simulator_contracts.py  shared dataclasses
  views/
    module_views.py               upload / extract / build-graph / review / graph
    interview_simulator_views.py  start / state / answer / abandon
    routing_simulation_views.py
    routing_diff_views.py         Colectica-vs-Forsta+ report / run / discrepancy-detail
    auth_views.py
mcp_server/
  models.py                      McpAccessToken (per-person access token)
  auth_middleware.py              TokenAuthMiddleware -- validates /t/<token>/mcp on every request
  tools.py                        the MCP tools (see "MCP tool server" above)
  prompts.py                      matching MCP prompts (slash-command shortcuts)
  server.py                       MCPServer instance, tool/prompt registration
  views.py / urls.py              staff-only token management UI (/questionnaires/mcp-tokens/)
  management/commands/runmcp.py   standalone streamable-HTTP server command
```

## Knowledge graph (graphify)

The codebase can be explored via [graphify](https://github.com/safishamsi/graphify), a tool that
turns the repo into a queryable knowledge graph (god nodes, community structure, cross-file
relationships) instead of relying on raw grep/browse. Output is written to `graphify-out/`
(gitignored — it's a regenerable local artifact, not committed source).

```bash
pip install graphifyy
graphify .                 # build the graph (AST + semantic extraction)
graphify query "<question>"          # BFS/DFS traversal, answers from the graph
graphify path "<A>" "<B>"            # shortest path between two concepts/symbols
graphify explain "<concept>"         # plain-language explanation of a node
graphify update .                    # incremental re-extract after code changes
```

`graphify-out/graph.html` opens as a standalone interactive visualization; `GRAPH_REPORT.md`
is a plain-language audit of god nodes, surprising connections, and suggested questions.

## Status / in-progress work

- `AgentRun`, `SyntheticProfile`, `SimulationRun`, `SimulationCase`, `ValidationIssue` models
  are defined but not yet wired into any view.
- The Forsta+ (Confirmit Horizons) XML import and Colectica-vs-Forsta+ routing-diff pipeline
  (see above) is implemented and in active use. See
  [`forsta_xml_routing_validation_plan.md`](forsta_xml_routing_validation_plan.md) for the
  original research doc and design rationale (it now also carries a "post-build" notes section
  documenting where the real implementation diverged from the initial design).
- `RoutingDiscrepancy.DiscrepancyType.CONDITION_MISMATCH` is defined on the model (for a future
  semantic/condition-evaluation diff, as opposed to the current structural diff) but not
  currently produced by `routing_comparator.py` — reserved, not a bug.
- An `mcp_server` tool for the latest `ModuleAIReview` result is designed but not yet built —
  intentionally on hold pending a separate go-ahead, not an oversight.
- `question_matcher.py` known limitation: two Colectica questions with byte-identical, generic
  reused wording (e.g. a form-letter follow-up like "And in which town is that?" asked in more
  than one routing context) can't be disambiguated by text similarity alone, so the wrong one can
  win a match. The name-tiebreak doesn't help here since the questions' *names* don't collide,
  only their text does. Not currently fixed — would need a different signal (e.g. routing-graph
  position) than text similarity.

## License

No license file yet — all rights reserved by default until one is added.
