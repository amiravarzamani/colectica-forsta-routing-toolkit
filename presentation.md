# Flowise Questionnaire System
### How It Works — System Overview

---

## Page 1 — What Is This System?

A **web application** that helps questionnaire designers work with
**Colectica-format survey JSON files** (e.g. UK longitudinal surveys like Understanding Society).

### The Problem It Solves
Colectica survey files are large, deeply nested JSONs.
Designers cannot easily:
- See which questions exist and how they are structured
- Understand the conditional routing logic (who gets asked what)
- Test whether all routing branches are reachable
- Simulate the experience of a real respondent going through the survey

### What the System Provides
| Capability | Description |
|---|---|
| Schema extraction | Parse every question, type, and answer option from raw JSON |
| Routing extraction | Extract all conditional, loop, and sequential flow rules |
| Graph visualization | Interactive node/edge map of the question flow |
| AI advisory review | AI analyses the routing structure and coverage gaps |
| Interview simulation | Walk through the questionnaire as a live respondent |

---

## Page 2 — Technology Stack

```
┌─────────────────────────────────────────────────────┐
│                   Browser (HTML/JS)                 │
│         Templates · Mermaid graph · AJAX            │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP
┌───────────────────────▼─────────────────────────────┐
│               Django 6.0.5  (Python)                │
│   Views · Services · Models · URL routing           │
│   Synchronous only — no Celery/async workers        │
└──────────┬───────────────────────┬──────────────────┘
           │                       │
┌──────────▼──────────┐   ┌────────▼─────────────────┐
│   PostgreSQL DB     │   │   Flowise AI Server       │
│ Stores all modules, │   │ (local at port 3000)      │
│ questions, edges,   │   │ Two agentflows:           │
│ sessions, reviews   │   │  · Module Review          │
└─────────────────────┘   │  · Interview Wording      │
                          └──────────────────────────┘
```

**Key constraint:** Django is the single source of truth.
Flowise is advisory only — it never controls routing or validation.

---

## Page 3 — Data Models

Five core models form the backbone of the system:

```
QuestionnaireModule          (one uploaded survey file)
        │
        ├──► NormalizedQuestion ×N    (every question extracted from the JSON)
        │         name, label, text, question_type, options_json, sequence_index
        │
        ├──► RoutingEdge ×M           (every routing rule extracted from the JSON)
        │         source_question, target_question, condition_text
        │         edge_type: conditional | sequential | loop
        │
        ├──► QuestionnaireGraph       (compiled visual graph — 1:1 with module)
        │         nodes_json, edges_json, mermaid_text
        │
        ├──► ModuleAIReview ×N        (each AI review run saved with validation result)
        │
        └──► InterviewSimulatorSession ×N
                  current_question_name, answers_json, routing_trace_json
                      │
                      └──► InterviewSimulatorTurn ×N
                                one record per question shown to respondent
```

---

## Page 4 — The Processing Pipeline

Every module goes through a **5-step pipeline**, enforced in order by the UI:

```
  STEP 1          STEP 2              STEP 3            STEP 4          STEP 5
  ┌──────┐     ┌──────────┐       ┌──────────┐       ┌────────┐     ┌──────────┐
  │Upload│────►│ Extract  │──────►│ Extract  │──────►│ Build  │────►│   Run    │
  │ JSON │     │ Schema   │       │ Routing  │       │ Graph  │     │AI Review │
  └──────┘     └──────────┘       └──────────┘       └────────┘     │(optional)│
                    │                   │                  │          └──────────┘
              NormalizedQuestion   RoutingEdge      QuestionnaireGraph
              records created      records created   + Mermaid text
```

### What each step does

| Step | Service | Output |
|---|---|---|
| Upload | Django form | Raw JSON saved to DB + disk |
| Extract Schema | `ColecticaSchemaExtractor` | NormalizedQuestion records (bulk) |
| Extract Routing | `ColecticaRoutingExtractor` | RoutingEdge records (conditional + loop + sequential) |
| Build Graph | `graph_builder` + `graph_enrichment` | Graph JSON + enriched start-semantics per node |
| AI Review | `flowise_client` → Flowise | ModuleAIReview saved with pass/fail validation |

---

## Page 5 — Schema & Routing Extraction

### Schema Extraction (`ColecticaSchemaExtractor`)
Recursively walks the nested Colectica JSON looking for any object that contains a `Question` key.
For each one it extracts:
- `name` (variable name, e.g. `AIDHH`)
- `text` (the actual question wording)
- `question_type` — detected from `ResponseDomains`: single_choice / multi_choice / number / text / date
- `options_json` — list of `{code, label, is_missing, exclusive}` dicts

### Routing Extraction (`ColecticaRoutingExtractor`)
Three passes over the same JSON:

| Pass | What it finds | Edge type |
|---|---|---|
| Conditional | `Branches` containers with `SourceCodeExpressions` | `conditional` |
| Loop | Activities whose name/label contains "loop" | `loop` |
| Sequential | Consecutive questions by sequence_index order | `sequential` |

**Condition text examples extracted:**
```
if [AIDHH = 1]
if [BENBASE IN 1,2,3]
if [HHGRID.HHSIZE - Number of absent hhold members) > 1]
NAIDXHH > 1 AND AIDXHH = 1
```

---

## Page 6 — AI Component 1: Module Review

The **Module Review agentflow** gives an advisory analysis of the extracted schema and routing.

### What Django sends to Flowise
```
{
  "schema_summary":   { question_names, question_types },
  "routing_summary":  { conditional_edges (indexed), external_variables },
  "coverage_intents": { up to 10 seed-answer test cases, one per conditional edge },
  "instructions":     { role: "advisory_review_only", strict rules list }
}
```

### What Django post-validates in the response
Django checks **every factual claim** Flowise makes:

| Check | What must match |
|---|---|
| `schema_review.real_question_names` | Exact list from Django |
| `routing_review.external_variables` | Exact list from Django |
| `routing_review.conditional_edge_count` | Exact count from Django |
| `coverage_intent_review.covered_conditional_edges` | Exact indexes from Django |
| Forbidden keys | `expected_active_questions` / `expected_skipped_questions` → rejected |

→ Any mismatch → review saved as **failed**.
→ All checks pass → review saved as **passed**.

---

## Page 7 — AI Component 2: Interview Wording

The **Interview Wording agentflow** formats each question into natural, respondent-facing language.

### Request (Django → Flowise)
```
question text + A/B/C option labels (NOT internal codes)
+ module name/version
+ previous answers (for context)
+ strict rules: do not expose variable names, do not invent options,
                do not route, preserve A/B/C letters exactly
```

### Response validation & repair pipeline
```
Flowise response
      │
      ├─ Contains internal variable name?  ──► FALLBACK (fatal)
      ├─ Exposes option codes?             ──► FALLBACK (fatal)
      ├─ Missing question text?            ──► FALLBACK (fatal)
      ├─ Missing any A/B/C letter?         ──► FALLBACK (fatal)
      ├─ Missing instruction line only?    ──► REPAIR (Django appends it)
      └─ All checks pass                  ──► USE response
```

### Caching
SHA-256 hash of (module, question text, options, selection_mode) → **24-hour cache**.
A repeated question never calls Flowise twice.

### Fallback
If Flowise is down or fails validation, Django formats the question **deterministically**:
`question text + A. label / B. label / ... + instruction line`
The simulator stays fully usable with zero AI dependency.

---

## Page 8 — Interview Simulator

The interview simulator lets a designer experience the questionnaire as a respondent.

### Session lifecycle
```
START SESSION
    │
    ▼
Pick first NormalizedQuestion (by sequence_index)
    │
    ▼
Build SimulatorQuestion          ← internal codes hidden; A/B/C letters assigned
    │
    ▼
Ask Flowise to format wording    ← or use deterministic fallback
    │
    ▼
Show question to respondent
    │
    ▼
Receive answer (e.g. "A" or "A, C" or "42")
    │
    ▼
Validate answer                  ← check letters valid, single vs multi, exclusivity
    │
    ▼
Convert A→code: {VARNAME: "1"}   ← respondent never sees internal codes
    │
    ▼
ROUTE to next question:
  ① Evaluate all outgoing conditional edges (condition_evaluator)
  ② First edge whose condition = true → go there
  ③ No true conditions → sequential fallback (scan forward, skip
     questions whose incoming gates are not satisfied)
    │
    ▼
Save InterviewSimulatorTurn      ← full audit trail per turn
Update InterviewSimulatorSession ← answers_json, routing_trace, graph highlight
    │
    ▼
Repeat until no next question → COMPLETED
```

---

## Page 9 — Key Design Principles & Summary

### The One Rule That Governs Everything
> **Django is authoritative. Flowise is advisory/formatting only.**

No AI output ever directly controls what the respondent sees next, validates an answer,
or determines which questions are active or skipped.

### How This Is Enforced
| Concern | Owner |
|---|---|
| Question extraction | Django (deterministic parser) |
| Routing logic | Django (condition_evaluator + interview_router) |
| Answer validation | Django (answer_validation) |
| Question ordering | Django (sequence_index) |
| Coverage testing | Django (coverage_intent_builder + routing_simulator) |
| Question wording | Flowise (with strict validation + fallback) |
| Schema/routing review | Flowise (advisory only, post-validated by Django) |

### System at a Glance
```
Colectica JSON
      ↓
  Extract (Django) ──────────────────────────────────────┐
      ↓                                                   │
  Graph (Django)                                         │
      ↓                                               [AI Review]
  Coverage Intents (Django)          [Wording]       Flowise #1
      ↓                             Flowise #2            │
  Interview Simulator ──────────────────┘           Post-validated
  (Django routing)                                  by Django
```

**Deployed at:** `flowise.example.com`
**Stack:** Django 6 · PostgreSQL · Flowise · Python 3.12
