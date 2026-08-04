# Forsta+ vs Colectica Routing Validation — Implementation Plan

**Status:** Steps 0-8 implemented, 65 tests passing (`python manage.py test`).

## Implementation notes (post-build)

- `ForstaXmlSchemaExtractor` (`services/forsta_xml_schema_extractor.py`) filters
  Hidden/Background questions out at extraction time (no NormalizedQuestion
  row is created for them at all), rather than storing a hidden flag and
  filtering later in the matcher. Simpler, and every stored NormalizedQuestion
  is automatically a text-matching candidate.
- **Real-file deviation from §3.2's assumption:** in the actual
  `p133375738246.xml`, `<Scale>` elements are used as answer-option tags
  (`Precode` + `Texts`, no `<Name>`/`<FormTexts>`) nested under other question
  types, not as standalone question wrappers. The extractor handles this
  safely (no `<Name>` child -> skipped, not a crash), so zero `NUMBER`-type
  questions come from `Scale` in this file. `Scale` stays in `QUESTION_TAGS`
  in case another export uses it as the plan originally described.
- Real-file counts (`p133375738246.xml`, via `ForstaXmlSchemaExtractor` /
  `ForstaXmlRoutingExtractor` run directly, not through Django): 2,113 visible
  questions (1,023 single / 687 multi / 361 text / 42 grid), 6,561 routing
  edges (2,881 conditional / 1,568 loop / 2,112 sequential), 69 of the
  conditional edges are negated (`NOT (...)`) FalseNodes-branch edges.
- `RoutingComparator` (Step 6) operates on every `QuestionMatch` with a
  `forsta_question` assigned, regardless of `confirmed` -- confirmation is
  human-review bookkeeping, not a gate on computing the structural diff.
- Report view lives at `routing-diff/<colectica_module_id>/<forsta_module_id>/`
  (`routing_diff_report_view`); `.../run/` (POST) recomputes matches +
  discrepancies via `build_question_matches` + `compare_routing_for_modules`.
**Purpose of this document:** so a future chat session (or a different person) can pick this
work up without re-deriving the file formats from scratch. Everything below was verified
directly against the two real files in this repo, not guessed.

## 1. The task, restated

Two files describe the *same* questionnaire (Understanding Society Mainstage Wave 18):

- `Understanding_Society_Mainstage_Wave_18_Questionnaire_-_Version_1.json` — the Colectica
  export from our own backend. This is what the app already knows how to parse.
- `p133375738246.xml` — the Forsta+ (Confirmit Horizons) export, produced by the fieldwork
  agency. We only ever receive this as a file; we have no API/system access to Forsta+.
  The app currently has **no parser for this format at all.**

The fieldwork agency implements routing independently from our Colectica-derived routing.
The goal is to validate their routing against ours: match questions between the two files
by question text, then compare the routing rules attached to matched questions and surface
discrepancies to the designer.

Two sub-questions the user asked to be resolved first:

1. Does the JSON side need a new "whole questionnaire" import path, distinct from the
   existing "module" import? → **Answered in §2: no.**
2. What does it take to extract questions + routing from the Forsta+ XML? → **Answered in
   §3-§4: a new extractor pair, structurally analogous to the existing Colectica ones.**

## 2. Finding: no separate "questionnaire" import is needed

`QuestionnaireModule` (the DB model) is really just "one uploaded questionnaire snapshot" —
nothing in the model or the pipeline restricts it to a single Colectica *module* (e.g. just
`demographics_w18`). It was only ever exercised with single-module files before.

`ColecticaSchemaExtractor` (`flowise_questionnaire/services/schema_extractor.py`) and
`ColecticaRoutingExtractor` (`flowise_questionnaire/services/routing_extractor.py`) both work
by recursively walking the *entire* JSON tree looking for `Question` / `Branches` / loop
patterns, with no assumption about how many modules are concatenated inside. This was
verified directly:

```
python probe script loading Understanding_Society_Mainstage_Wave_18_Questionnaire_-_Version_1.json (120 MB)
  json.load:              0.66s
  ColecticaSchemaExtractor.extract_questions():  0.47s  -> 1,803 questions
  ColecticaRoutingExtractor.extract_edges():     0.69s  -> 4,152 edges
    (1,802 sequential / 1,428 conditional / 922 loop)
```

Questions and edges span every submodule listed in the file's `StudyAbstract` (hhgrid,
household, demographics, benefits, politics, etc.) — confirming this already behaves as a
whole-questionnaire import, not a single-module one.

**Conclusion: upload the full JSON as-is through the existing `module_upload_view` /
`module_extract_schema_view` / `module_extract_routing_view` flow. No new import path,
model, or extractor is needed on the JSON side.**

Caveats worth flagging (not blockers, just things to watch):

- `graph_builder.py` / `graph_enrichment.py` only read `NormalizedQuestion` / `RoutingEdge`
  rows, never `raw_json` — confirmed by grep. So the graph and interview simulator will work
  unmodified at whole-questionnaire scale.
- The Module Review Flowise agentflow (`agentflow_payload_builder.py`) was designed and
  tuned around single-module payloads. At ~1,800 questions / ~4,200 edges the payload is
  much bigger than anything it's been tested with — likely too large for one LLM call
  without chunking. Not needed for the routing-validation work below; flag separately if/when
  someone wants to run the AI review over a whole-questionnaire module.
- `raw_json` is stored whole in a Postgres `JSONField` (~120 MB per upload). Fine for
  one-off use; would bloat the DB across many repeated uploads/versions.

## 3. Forsta+ / Confirmit Horizons XML format — reference notes

This is a **Confirmit Horizons** project export (see `WI_Url="https://horizons.confirmit.eu/..."`
in the file), referred to by the user as "Forsta+" output (Forsta acquired/rebranded
Confirmit). 14 MB, 31,344 lines, single root `<Project>`.

### 3.1 Tree shape

Everything questions/routing-related lives under one recursive container, confirmed via
streaming parse of the ancestor path to the first `<Page>` element:

```
Project > Questionnaire > Routing > Nodes > (Folder | Condition | Loop | Page | Script | Single | Multi | Open | Scale | Grid)*
```

`<Nodes>` is the generic child container — directly analogous to Colectica's
`Activities`/`ChildSequence`. It can hold, in document order, any mix of:

- `<Page>`, `<Folder>` — pure grouping/visual containers (own nested `<Nodes>`)
- `<Condition>` — conditional branch (see 3.3)
- `<Loop>` — repeating block (see 3.4)
- `<Script>` — side-effect-only logic (sets hidden variables, no respondent-facing text)
- `<Single>`, `<Multi>`, `<Open>`, `<Scale>`, `<Grid>` — question elements (see 3.2)

### 3.2 Question elements

Tag name **is** the question type (unlike Colectica, where everything is a generic
`Question` object with a `RepresentationType`). Structure:

```xml
<Single EntityId="14314" VariableType="..." ...>
  <FormTexts>
    <FormText Language="9">
      <Title>Confirm absent at university</Title>
      <Instruction />
      <Text>Last time, ^f('ABSUNLoop')^ was away...</Text>
    </FormText>
    <FormText Language="512">...(Welsh translation)...</FormText>
  </FormTexts>
  <Name>ABSUN_confirm</Name>
  <SingleAnswers EntityId="...">
    <Answer Precode="1"><Texts><Text Language="9">Yes</Text>...</Texts></Answer>
    ...
    <!-- OR, when the list is shared/reused: -->
    <Predefined ListSource="8348" ReferencedEntityId="8348" />
  </SingleAnswers>
</Single>
```

Key facts, all verified against the file:

- **Variable name** → `<Name>` (equivalent of Colectica `ItemName`).
- **Question text** → `FormTexts/FormText[@Language='9']/Text` (fall back to `Title` when
  `Text` is empty — many "Hidden" questions only have a `Title`, no respondent text).
  `<DefaultLanguage>9</DefaultLanguage>` is declared elsewhere in the file (confirmed under
  an `<Email>` node) and English text under `Language="9"` was confirmed by inspection
  (e.g. `Language="512"` consistently carries the Welsh translation). **Use Language 9 as
  the English/primary text**, same role as `en-GB` in the Colectica extractor.
- **Answer options** → `<SingleAnswers>` / `<MultiAnswers>` / `<ScaleAnswers>` /
  `<GridAnswers>`, containing either inline `<Answer Precode="...">` elements (code + label
  per language), or a `<Predefined ListSource="N" ReferencedEntityId="N" />` pointer to a
  **shared answer list defined elsewhere in the document** as `<PredefinedList EntityId="N">`
  (own `<PredefinedListAnswers>` with the same `<Answer Precode>` shape). Resolving these
  requires a first pass over the whole file to build an `EntityId -> PredefinedList` lookup
  table before walking the question tree — Colectica has no equivalent of this (options are
  always inline there).
- **Visibility**: question elements carry `VariableType="Hidden"` or `VariableType="Background"`
  when they're internal/system variables, and no `VariableType` attribute when respondent-
  visible. Counts in this file: 1,701 hidden/background vs 3,345 visible question elements
  (4,561 distinct `<Name>`s total across question tags). This is a much higher
  hidden-variable ratio than the Colectica side and **must** be filtered out (or at least
  down-weighted) before text-matching, or hidden plumbing variables will pollute the match
  set.
- Question text and answer labels routinely contain **piping syntax**
  (`^f('names_only').item('32').get()^`) and inline HTML (`<br/>`, `<b>`). These need to be
  stripped/normalized before text comparison — same category of problem the Colectica side
  already has with `{if HHGrid.NAMEPERM = 1}`-style piping in `QuestionText`, just a
  different syntax. Check whether `question_presentation.py` already has reusable
  normalization logic before writing a new one.
- **Grid** questions (131 in this file) are multi-row/multi-column matrix questions with no
  clean equivalent in `NormalizedQuestion.QuestionType` (single_choice / multi_choice / text
  / number / date / unknown). Needs an explicit decision — see §6 open questions.

### 3.3 Conditional routing

```xml
<Condition EntityId="14909" PerformDelete="false" ElseEnabled="false" ReadOnly="false">
  <Expression>f('hhGridStatus').none('3') &amp;&amp; f('hhRecord').any('1')</Expression>
  <Predicate><Predicates /></Predicate>
  <TrueNodes EntityId="14909_TrueNodes">
    ... questions/scripts/nested conditions shown when Expression is true ...
  </TrueNodes>
  <FalseNodes EntityId="14261_FalseNodes" />  <!-- often empty, but NOT always -->
</Condition>
```

- `<Expression>` is a **JavaScript-like boolean expression** referencing fields as
  `f('VarName')` with methods like `.any('x')`, `.none('x')`, `.toBoolean()`, and
  `&&`/`||` operators — a completely different syntax from Colectica's bracketed
  `[A = 1 | 2]` / pipe-condition DSL that `condition_evaluator.py` already parses.
  **Cannot reuse `condition_evaluator.py` as-is**; a new evaluator would be needed for
  semantic (not just structural) comparison — see Step 8 below.
- `<TrueNodes>` holds the nodes activated when the condition is true — this is the
  Forsta+ analogue of Colectica's `Branches[].ChildSequence.Activities`.
- **Important structural difference from Colectica**: `<FalseNodes>` can be **non-empty**,
  i.e. Forsta+ supports an explicit "else, show these different questions" branch. Colectica
  has no equivalent — a false/unmatched condition there just means "fall through to the next
  question in sequence" (confirmed by reading `interview_router.py`:
  `_is_question_eligible_by_incoming_conditions` — a question with no true incoming
  conditional edge is simply skipped, there's no modeled "else" target). This asymmetry is a
  real candidate source of routing mismatches between the two systems and should be called
  out explicitly in the comparison output, not silently normalized away.

### 3.4 Loop routing

```xml
<Loop EntityId="14316" FieldWidth="-1">
  <LoopMembers><Predefined ListSource="9205" ReferencedEntityId="9205" /></LoopMembers>
  <Nodes>
    ... question elements repeated once per loop member ...
  </Nodes>
</Loop>
```

Structurally maps directly onto what `ColecticaRoutingExtractor._extract_loop_edges` already
does for Colectica's `Loop`/`LoopWhile`/`LoopUntil` containers: loop container name → every
question directly/recursively contained in it, edge_type=`loop`.

### 3.5 Sequential order

Not an explicit construct in the XML — same as Colectica, derive it from document order of
question elements within `<Nodes>` (mirrors `ColecticaRoutingExtractor._extract_sequential_edges`,
which builds sequential edges from `NormalizedQuestion.sequence_index` rather than from the
source file directly).

## 4. Colectica JSON ↔ Forsta+ XML concept mapping

| Concept | Colectica JSON | Forsta+ XML |
|---|---|---|
| Generic child container | `Activities` / `ChildSequence` | `Nodes` |
| Question wrapper | any object with a `Question` key | tag *is* the type: `Single`/`Multi`/`Open`/`Scale`/`Grid` |
| Variable name | `Question.ItemName` | `Name` |
| Question text (multilingual) | `QuestionText` dict, key `en-GB` | `FormTexts/FormText[@Language='9']/Text` (fallback `Title`) |
| Answer options | `ResponseDomains[].Codes.Codes[]`, always inline | `*Answers/Answer` inline, **or** `Predefined ListSource=N` pointing at a global `PredefinedList` needing a resolve pass |
| Conditional branch | `Branches[].Condition.SourceCodeExpressions[].Code` + `ChildSequence.Activities` (true-branch only, implicit else = skip) | `Condition/Expression` + `TrueNodes` **and optionally non-empty `FalseNodes`** (explicit else) |
| Condition syntax | bracketed DSL, e.g. `[A = 1 \| 2]`, parsed by `condition_evaluator.py` | JS-like, e.g. `f('A').any('1')`, no existing parser |
| Loop | `Loop`/`LoopWhile`/`LoopUntil` + `Activities` | `Loop` + `LoopMembers` + `Nodes` |
| Hidden/system variables | not distinguished from real questions in the extractor | explicit `VariableType="Hidden"`/`"Background"` attribute — 1,701 of 4,561 names |

## 5. Data model plan

Reuse `QuestionnaireModule` / `NormalizedQuestion` / `RoutingEdge` rather than inventing a
parallel model tree — the whole point of `NormalizedQuestion`/`RoutingEdge` being
source-agnostic is that `graph_builder.py`, `graph_enrichment.py`, `interview_router.py`, etc.
all already operate purely on those tables and don't care how they were populated.

- `QuestionnaireModule`: add `source_format` (`TextChoices`: `colectica_json` default,
  `forsta_xml`), and a `raw_xml` field (`TextField`, null/blank) alongside the existing
  `raw_json`. Upload/extract views branch on `source_format` to call the right extractor.
  Each XML upload becomes its own `QuestionnaireModule` row — matching/comparison then
  operates across a **pair** of module ids (one Colectica, one Forsta+), not within one.
- New `ForstaXmlSchemaExtractor` / `ForstaXmlRoutingExtractor` produce the exact same
  `ExtractedQuestion` / `ExtractedRoutingEdge` dataclasses the Colectica extractors already
  produce, so `extract_schema_for_module` / `extract_routing_for_module` need only a small
  branch, not a rewrite.
- New `QuestionMatch` model: `colectica_question` FK, `forsta_question` FK, `match_score`
  float, `match_method` (`exact`/`fuzzy`/`manual`), `confirmed` bool, `confirmed_by` FK user
  nullable — lets a designer review/correct automatic matches in the UI rather than trusting
  them blindly.
- New `RoutingDiscrepancy` model: `question_match` FK, `discrepancy_type`
  (`missing_in_forsta` / `missing_in_colectica` / `condition_mismatch` / `else_branch_only_in_forsta`
  / ...), nullable FKs to the specific `colectica_edge` / `forsta_edge` involved,
  `details_json`, `severity`.

## 6. Decisions (confirmed)

1. **Grid questions** — Confirmit `Grid` (131 instances) gets a new `GRID`/`MATRIX` value on
   `NormalizedQuestion.QuestionType`. Not decomposed into synthetic per-cell questions.
2. **Explicit `FalseNodes`** — when non-empty, modeled as a second conditional `RoutingEdge`
   with a negated condition (`NOT (...)`) sourced from the same question. Not a new edge type.
3. **Depth of comparison** — ship the fast structural diff first (edge presence/count/source-
   variable-set diff per matched question, no semantic evaluation of condition syntax).
   Semantic comparison waits for Step 8 (the full condition evaluator).
4. **Model reuse vs new tables** — confirmed: extend `QuestionnaireModule` directly with
   `source_format` (§5 approach), not a wholly separate app/model tree for fieldwork imports.

## 7. Step-by-step roadmap

Each step is independently shippable and testable.

- **Step 0 — done.** Confirmed whole-questionnaire JSON import already works via the
  existing module pipeline (§2). No code changes needed for this step; documented here so
  it isn't re-investigated.
- **Step 1.** Migration: add `source_format` + `raw_xml` to `QuestionnaireModule`.
- **Step 2.** `ForstaXmlSchemaExtractor`: two-pass parse — pass 1 builds
  `EntityId -> PredefinedList` lookup across the whole document; pass 2 walks
  `Questionnaire/Routing/Nodes` recursively, extracting `Single`/`Multi`/`Open`/`Scale`/`Grid`
  into `ExtractedQuestion` objects (name, text with piping/HTML stripped, type, options
  resolved via the lookup table when `Predefined` is used, hidden/background flag surfaced
  for later filtering).
- **Step 3.** `ForstaXmlRoutingExtractor`: same recursive walk, extracting `Condition`
  (`TrueNodes` always, `FalseNodes` per the §6.2 decision), `Loop`, and sequential edges,
  mirroring `ColecticaRoutingExtractor`'s method shapes so the two extractors stay easy to
  read side by side.
- **Step 4.** Upload form + view changes to accept `.xml`, branch extraction by
  `source_format`. `module_detail_view`, graph build/enrichment, and the interview simulator
  should need zero changes since they only touch `NormalizedQuestion`/`RoutingEdge` — verify
  this holds once real data is in the tables.
- **Step 5.** `question_matcher.py` + `QuestionMatch` model/migration: normalize text on both
  sides (strip HTML/piping, lowercase, collapse whitespace), exact match first, then fuzzy
  match with a score threshold for the remainder, filtering out Forsta+ `Hidden`/`Background`
  questions from the candidate pool. Manual review UI for anything below the confidence
  threshold or entirely unmatched.
- **Step 6.** `routing_comparator.py` + `RoutingDiscrepancy` model/migration: for each
  confirmed `QuestionMatch`, diff incoming/outgoing routing edges structurally (presence,
  count, source-variable-set) and persist discrepancies with severity.
- **Step 7.** Report view: side-by-side or diff-list UI, filterable by discrepancy type, one
  module pair at a time.
- **Step 8 (stretch).** `ForstaConditionEvaluator` mirroring `condition_evaluator.py` for the
  `f('var').any(...)`/`&&`/`||` syntax, enabling actual simulated-path comparison (same
  synthetic answers fed through both routing engines, compare which questions each system
  would actually show) — reuses the synthetic seed generation already built in
  `coverage_intent_builder.py` / `routing_simulator.py` for the Colectica side.
- **Step 9 (stretch).** Visual overlay on the existing graph view highlighting where the two
  routing graphs diverge.

## 8. How this research was done (for reproducibility)

No code was written yet — this was pure investigation, done by:

- Reading `models.py`, `schema_extractor.py`, `routing_extractor.py`, `graph_builder.py`,
  `graph_enrichment.py`, `condition_evaluator.py`, `interview_router.py`.
- Running the real Colectica extractors directly (not through Django/DB) against the full
  120 MB JSON file to measure timing and output counts (see §2 numbers).
- Streaming-parsing the XML with `xml.etree.ElementTree.iterparse` to find the ancestor path
  to the tree root, and targeted regex/substring probes to pull representative samples of
  `Page`, `Single`, `Condition`/`TrueNodes`/`FalseNodes`, `Loop`, and `PredefinedList`
  elements, plus tag-frequency counts and `VariableType` counts across the whole file.

Both source files (`p133375738246.xml`,
`Understanding_Society_Mainstage_Wave_18_Questionnaire_-_Version_1.json`) are already in the
repo root and were used as the ground truth throughout.
