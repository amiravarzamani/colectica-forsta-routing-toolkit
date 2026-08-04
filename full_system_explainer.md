# What the System Actually Does — Both Sides Explained
## For Management: The Non-AI Code and The AI Code

---

## The Two Actors in This System

This system has two active components doing real work:

1. **Django (our application code)** — runs on our server, written in Python,
   does the heavy lifting: reads files, stores data, evaluates logic, routes respondents

2. **Flowise (the AI server)** — a separate AI service running locally on the same server,
   receives structured requests from Django, returns natural language text

They work together but have completely separate responsibilities.
Below is a full account of what each one actually does, in the order things happen.

---

# PART 1 — WHAT THE NON-AI CODE (DJANGO) DOES

---

## Step 1: Receiving and Storing the Uploaded File

When a designer uploads a Colectica JSON file through the web interface:

- Django receives the file via an HTML form
- It checks that the file is a `.json` file and is under 50 MB
- It saves the file to disk under `media/flowise_questionnaires/raw_json/`
- It reads the entire file content into a JSON field in the database (`raw_json`)
- It creates a `QuestionnaireModule` database record with status `uploaded`

At this point nothing has been analysed yet. The JSON is just stored.

---

## Step 2: Extracting the Questions (Schema Extraction)

**Code: `services/schema_extractor.py` — `ColecticaSchemaExtractor`**

Django reads the raw JSON from the database and walks through it recursively.
The JSON is a deeply nested tree. The extractor visits every single node in that tree.
Whenever it finds an object that has a `Question` key inside it, it stops and reads:

**Question name:**
It looks for `ItemName` inside the `Question` object. This is the internal variable name
that the survey uses to identify this question (e.g. `AIDHH`, `BENBASE`, `FRVAL`).

**Question text:**
It looks for `QuestionText` or `Summary` inside the `Question` object.
This is the actual wording that would be read to a respondent
(e.g. "Does anyone in your household provide regular care or support...").

**Question type:**
It inspects the `ResponseDomains` field to decide what kind of answer this question expects:
- If it has a `Codes` field with `MultipleChoiceType = 0` → single-choice question
- If it has a `Codes` field with `MultipleChoiceType = 1` → multi-choice question
- If it has a `NumericType` or `RepresentationType = 1` → number question
- If `RepresentationType = 2` → date question
- Otherwise → text question

**Answer options:**
For choice questions, it reads every `Code` entry under `ResponseDomains.Codes.Codes`.
For each code it extracts:
- `code` — the internal answer value (e.g. `"1"`, `"2"`, `"96"`)
- `label` — the visible text (e.g. `"Yes"`, `"No"`, `"Don't know"`)
- `is_missing` — whether this is a special missing value like Refused or Don't know
- `exclusive` — whether selecting this option means no other options can be selected

**Multilingual handling:**
Question text and labels can appear as `{"en-GB": "...", "en-US": "..."}` dictionaries.
The extractor always prefers `en-GB`, then `en-US`, then any English variant it finds.

**Deduplication:**
If the same question name appears more than once in the JSON (which can happen in
Colectica exports), only the first occurrence is kept. Duplicates are silently skipped.

**Output:**
All extracted questions are saved to the database in a single bulk operation.
Each one becomes a `NormalizedQuestion` record with a `sequence_index` that records
the order they were found in the JSON tree.

---

## Step 3: Extracting the Routing Rules

**Code: `services/routing_extractor.py` — `ColecticaRoutingExtractor`**

Django walks the same JSON a second time, this time looking for routing rules.
There are three types:

### Type A — Conditional edges (the if-then rules)

The extractor finds every `Branches` object anywhere in the JSON tree.
Each branch has:
- A `Condition` with `SourceCodeExpressions` — the condition text
  (e.g. `if [AIDHH = 1]`, `if [BENBASE IN 1,2,3]`, `if [NAIDHH > 1 AND AIDHH = 1]`)
- A `ChildSequence` with `Activities` — the questions to show if the condition is true

For each branch, the extractor:
1. Reads the condition text from `SourceCodeExpressions`
2. Scans the condition text for variable names using regex patterns
3. Matches those names against the list of known question names to identify
   which question is the source (whose answer drives the condition)
4. Reads the target questions from the branch's `ChildSequence.Activities`
5. Saves a `RoutingEdge` record for each source → target pair:
   ```
   source_question:  "AIDHH"
   target_question:  "NAIDHH"
   condition_text:   "if [AIDHH = 1]"
   edge_type:        "conditional"
   ```

### Type B — Loop edges

The extractor finds activity objects whose name, label, or header contains the word "loop".
These represent sections of the questionnaire that repeat (e.g. one block of questions
per household member). For each loop it records which questions are contained inside it
as `loop` type edges.

### Type C — Sequential edges

After all conditional and loop edges are recorded, the extractor generates one sequential
edge between every consecutive pair of questions (question 1 → question 2, etc.)
based on their `sequence_index`. These represent the default flow when no conditional
rule applies.

**Output:**
All edges are saved to the database as `RoutingEdge` records.

---

## Step 4: Building the Graph

**Code: `services/graph_builder.py` and `services/graph_enrichment.py`**

Django takes all the `NormalizedQuestion` and `RoutingEdge` records and builds a graph.

**graph_builder** creates:
- A `nodes_json` list — one node per question, plus extra nodes for loop containers
  and external variables (variables referenced in conditions but not questions in this module)
- An `edges_json` list — one edge per routing rule, carrying the condition text as a label
- A `mermaid_text` string — the same graph in Mermaid diagram syntax so it can be
  rendered visually in the browser

**graph_enrichment** then enriches each node with:
- `incoming_routes` — which edges point TO this question
- `outgoing_routes` — which edges lead FROM this question
- `prerequisite_questions` — which questions' answers must have been collected
  for any incoming condition to be evaluable
- `start_semantics` — whether this question is the true first question of the module,
  or whether it depends on external variables, or has incoming conditions

**Output:**
One `QuestionnaireGraph` record saved to the database, containing the full graph JSON
and the Mermaid text. This powers the visual graph on the module detail page.

---

## Step 5: Building Coverage Intents (Test Cases)

**Code: `services/coverage_intent_builder.py`**

Before the AI review is run (and independently of it), Django generates its own
test cases for every conditional routing edge. These are called "coverage intents".

For each conditional edge, Django asks: what answer would a respondent need to give
to make this condition evaluate to true?

It reads the condition text and infers the answer using pattern matching:
- `AIDHH = 1` → seed answer: `{AIDHH: "1"}`
- `NAIDHH > 1` → seed answer: `{NAIDHH: 2}` (threshold + 1)
- `BENBASE IN 1,2,3` → seed answer: `{BENBASE: "1"}` (first value in the list)
- `AIDHH <> 2` → seed answer: `{AIDHH: "1"}` (any value that is not 2)

It also checks: if the source question is itself behind a conditional gate,
what upstream answer is needed to even reach that source question?
It adds those upstream answers to the seed set automatically.

The result is up to 10 coverage intent records, each describing:
- Which conditional edge it is trying to trigger
- What seed answers would be needed
- What external variables are also required
- Notes about special cases in the condition

These coverage intents are used both by the routing simulation and by the AI review payload.

---

## Step 6: Routing Simulation (Testing Without AI)

**Code: `services/routing_simulator.py`**

Django can run all the coverage intents deterministically against all conditional edges.
For each intent it:
1. Takes the seed answers
2. Evaluates every single conditional edge in the module against those answers
3. Records which edges evaluated to true (triggered) and which evaluated to false
4. Identifies the specific edge the intent was trying to cover and reports
   whether it was actually triggered

This gives the designer a full picture of which routing branches are reachable
and which ones have missing inputs or unsatisfied conditions — entirely without AI.

---

## Step 7: Evaluating a Routing Condition

**Code: `services/condition_evaluator.py`**

This is the engine that decides "does this condition evaluate to true given these answers?"
It is used both in the routing simulation and in the live interview simulator.

It takes a condition text string and a dictionary of current answers, and returns
one of: `true`, `false`, `unknown`, or `unsupported`.

How it parses conditions:
1. Strips the `if [...]` wrapper
2. Splits the expression on top-level `AND` / `OR` (respecting parentheses depth)
3. For each atomic sub-expression, matches one of these patterns:
   - `VAR = VALUE` → checks if the answer for VAR equals VALUE
   - `VAR <> VALUE` → checks if the answer for VAR does not equal VALUE
   - `VAR > VALUE` → checks if the answer for VAR is numerically greater than VALUE
   - `VAR >= VALUE` → checks if the answer for VAR is numerically at least VALUE
   - `VAR IN 1,2,3` → checks if the answer for VAR is in the list
   - `VAR = 1|2|3` → same as IN, pipe-separated variant
   - Special household expression → handled with a documented approximation
4. For AND: all parts must be true → result is true
5. For OR: any part being true → result is true
6. If a required answer has not been collected yet → `unknown`
7. If the expression cannot be parsed safely → `unsupported`

Variable name matching is case-insensitive. `AidHH` and `AIDHH` match the same answer.
For multi-choice questions, the answer is a list of codes, and membership is checked
against the full list (any selected code matching the condition counts as true).

---

## Step 8: Running the Live Interview Simulator

**Code: `services/interview_simulator_service.py` and `services/interview_router.py`**

When a designer starts a simulation session:

**Starting:**
- Django finds the first question by lowest `sequence_index`
- Creates an `InterviewSimulatorSession` record with:
  - `current_question_name` = first question's name
  - `answers_json` = empty dictionary
  - `routing_trace_json` = empty list (grows as the interview progresses)
  - `graph_highlight_json` = first node highlighted

**Presenting a question:**
- Django loads the `NormalizedQuestion` from the database
- Converts it into a `SimulatorQuestion` object (see Step 8a below)
- Passes it to the AI for wording (see Part 2)
- Returns the formatted question to the browser

**Receiving an answer:**
- Django receives the raw respondent input (e.g. `"A"`, `"A, C"`, `"42"`)
- Passes it to the answer validator

**Validating the answer — `services/answer_validation.py`:**

For choice questions:
- Splits the input on spaces, commas, semicolons, slashes, pipes
- Normalises each token to uppercase (`"a"` → `"A"`)
- Checks every letter against the known option letters for this question
- Rejects if any letter is unrecognised
- Rejects if the question is single-choice but multiple letters were submitted
- Checks for exclusive options: if an exclusive option (e.g. "None of these")
  is selected alongside other options → rejected
- Converts accepted letters to internal codes:
  - `"A"` → `{AIDHH: "1"}` (A was assigned to code 1 at presentation time)
  - `"A, C"` → `{BENBASE: ["1", "3"]}` (multi-choice → list of codes)

For number questions:
- Checks that the input matches a numeric pattern (integer or decimal)
- Stores the raw number as the coded answer

For text questions:
- Checks that the input is not empty
- Stores the raw text as the coded answer

**Routing to the next question — `services/interview_router.py`:**

With the new coded answer added to the session's `answers_json`:
1. Collect all routing edges where `source_question` matches the current question
2. For each conditional edge: call `condition_evaluator` with all collected answers
3. If any conditional edge evaluates to `true`: go to that edge's target question
   (the first true edge in sequence order wins)
4. If no conditional edge is true: scan forward through questions by sequence_index
   - For each candidate question: check if it has incoming conditional edges
   - If it does: evaluate those conditions; if none are true → skip this question
   - If it has no incoming conditional edges: it is eligible → go here
5. If the scan reaches the end with no eligible question: session is complete

**Saving the turn:**
After each answer, Django saves a complete `InterviewSimulatorTurn` record containing:
- The question that was shown
- The options that were displayed (with A/B/C letters)
- The raw text the respondent typed
- The validated letter selections
- The coded answer (internal codes)
- The routing result (which edge was selected, which were rejected, why)
- The graph highlight state (which node is now active)
- The full AI request and response (for audit)

The `InterviewSimulatorSession` is also updated:
- `current_question_name` advances to the next question
- `answers_json` accumulates all collected coded answers
- `routing_trace_json` appends a record of this routing decision with timestamp

---

## Step 8a: Converting a Question for Respondent Display

**Code: `services/question_presentation.py`**

Before a question can be shown to a respondent or sent to the AI, Django converts
the internal `NormalizedQuestion` into a `SimulatorQuestion`.

The key transformation is assigning A/B/C letters to answer options:

The internal question stores answer codes like `"1"`, `"2"`, `"3"`, `"96"`, `"97"`.
Respondents must never see these codes. So Django assigns fresh letters in order:
- Code `"1"` (Yes) → letter `A`
- Code `"2"` (No) → letter `B`
- Code `"96"` (Don't know) → letter `C`

This mapping exists only for the duration of one question presentation.
When the respondent answers `"A"`, Django looks up that A maps to code `"1"`,
and stores `{AIDHH: "1"}` as the coded answer.

Missing-value options (Don't know, Refused, Inapplicable) are filtered out by default
so respondents only see the substantive answer choices.

For questions with more than 26 options, the lettering continues as `AA`, `AB`, `AC`, etc.

---

# PART 2 — WHAT THE AI (FLOWISE) ACTUALLY DOES

The AI has exactly two jobs. Both are narrow and tightly controlled by Django.

---

## AI Job 1: Reviewing the Module Structure

**When it runs:** Only when a designer manually clicks "Run AI Review".
It never runs automatically.

**What Django sends to the AI:**

Django builds a structured JSON payload and sends it to the Flowise server.
The payload contains:
- The list of all question names and their types
- All conditional edges, each numbered with an index
- The list of external variables referenced in conditions
- The coverage intent test cases that Django already built
- A strict instruction block

The instruction block explicitly tells the AI:
- Your role is "advisory_review_only"
- Copy the question names exactly as given — do not invent any
- Copy the conditional edge count exactly as given
- Copy the covered and uncovered edge indexes exactly as given
- Do not produce `expected_active_questions` or `expected_skipped_questions`

**What the AI does with this:**

The AI reads the survey structure and writes a review. It looks at things like:
- Are there questions with no label or empty text?
- Are there conditional routes that look like they might never be triggered?
- Are there conditions that reference external variables that might not always be available?
- Do the coverage intents seem sufficient to exercise all routing paths?
- Are there any observations or concerns the designer should know about?

The AI writes its response as structured JSON with sections:
`schema_review`, `routing_review`, `coverage_intent_review`, `suggested_improvements`

**What Django does with the AI's response:**

Django reads the response and validates every factual claim against its own database records:
- Are the question names the AI listed identical to the ones Django sent? If not → fail
- Is the conditional edge count the AI reported equal to Django's count? If not → fail
- Are the covered edge indexes the AI listed identical to Django's list? If not → fail
- Does the response contain any forbidden keys? If yes → fail

If validation fails: the review is saved with status `failed` and the errors are shown.
If validation passes: the review is saved with status `passed`.

The designer reads the AI's observations as advisory input.
Nothing in the AI's review changes the routing logic, the questions, or the test cases.

---

## AI Job 2: Formatting a Question for the Respondent

**When it runs:** Every time a question is about to be shown during an interview simulation.

**What Django sends to the AI:**

- The question text (the actual survey wording)
- The answer options with A/B/C letters and visible labels — no internal codes
- The module name and version
- Previous answers collected so far (for conversational context)
- Routing context (previous question name, current question name)
- A strict list of rules the AI must follow:
  - Ask only the real question text
  - Do not expose internal variable names
  - Do not invent, remove, reorder, or rename options
  - Preserve the exact A/B/C option letters
  - Do not validate answers
  - Do not choose the next question
  - Do not mention routing
  - Do not mention option codes
  - Return only the respondent-facing assistant message

**What the AI does with this:**

The AI takes the raw question text and the lettered options and writes them up
in a natural, conversational way — the way a professional interviewer would phrase
the question when reading it to someone.

For example, given:
```
text:    "Does anyone in your household provide regular care..."
options: A. Yes   B. No
mode:    single choice
```

The AI might return:
```
I'd now like to ask about caring responsibilities in your household.

Does anyone in your household provide regular care or support
to a family member, friend or neighbour?

A. Yes
B. No

Choose one option only. Answer using one letter only, for example: A.
```

**What Django does with the AI's response:**

Before showing anything to the respondent, Django runs five checks in order:

1. **Variable name check** — does the response contain the internal variable name
   (e.g. the word `AIDHH`)? If yes: reject the AI response. Use the fallback instead.

2. **Option code check** — does the response contain phrases like "code 1" or
   "option code: 3"? If yes: reject. Use the fallback instead.

3. **Question text check** — does the response contain at least the first 60 characters
   of the actual question text? If no: reject. Use the fallback instead.

4. **Option letter check** — does the response show every single A/B/C letter that
   was sent (in `A.` or `A)` format)? If any is missing: reject. Use the fallback instead.

5. **Instruction line check** — does the response contain the correct instruction
   (e.g. "Choose one option only. Answer using one letter only, for example: A.")?
   If missing: this is the one thing Django repairs by appending it automatically.
   It is not a fatal failure.

If checks 1 to 4 all pass (with or without the instruction line repair): show the AI version.
If any of checks 1 to 4 fail: show the deterministic fallback instead.

**What the deterministic fallback is:**

It is a simple local function that Django runs with no AI and no network call:

```
Does anyone in your household provide regular care or support
to a family member, friend or neighbour?

A. Yes
B. No

Choose one option only. Answer using one letter only, for example: A.
```

It is called "deterministic" because it is produced by a fixed function.
The same question always produces the exact same output every time, with no variation.
"Fallback" means it is only used when the AI path fails or is unavailable.

The interview simulation never stops working because of an AI failure.
The question still appears correctly. The routing still works. Answers are still recorded.
Only the conversational polish from the AI is missing.

**Caching:**

The system does not call the AI every single time the same question appears.
It computes a fingerprint (SHA-256 hash) of the question content:
module name + module version + question text + options + selection mode.
If this fingerprint was seen less than 24 hours ago, the cached AI response is returned
instantly without contacting Flowise at all.

---

# SUMMARY — THE WHOLE SYSTEM IN ONE FLOW

```
Designer uploads Colectica JSON file
            │
            ▼
DJANGO receives file, validates it, saves to disk and database
            │
            ▼
DJANGO walks the JSON tree recursively
→ finds every Question object
→ extracts name, text, type, answer options
→ saves NormalizedQuestion records to database
            │
            ▼
DJANGO walks the JSON tree a second time
→ finds every Branch object (if-then rules)
→ extracts condition text and target questions
→ finds loop structures
→ generates sequential pairs from question order
→ saves RoutingEdge records to database
            │
            ▼
DJANGO compiles questions + edges into a visual graph
→ assigns incoming/outgoing routes to each node
→ classifies start semantics per node
→ saves QuestionnaireGraph (nodes JSON, edges JSON, Mermaid text)
            │
            ▼
DJANGO generates coverage intent test cases
→ one per conditional edge, with inferred seed answers
→ up to 10 intents total
            │
            ├──────────────────────────────────────────────────────┐
            │  [Optional: AI Module Review]                        │
            │  DJANGO builds compact payload                       │
            │  (question names, edges, coverage intents, rules)    │
            │  → sends to FLOWISE AI                              │
            │  → AI writes advisory schema and routing review      │
            │  → DJANGO validates every factual claim in response  │
            │  → saves ModuleAIReview as passed or failed          │
            └──────────────────────────────────────────────────────┘
            │
            ▼
DJANGO runs routing simulation (no AI)
→ evaluates all coverage intents against all conditional edges
→ reports which branches are triggered / not triggered / missing inputs
            │
            ▼
Designer starts Interview Simulation
            │
            ▼
DJANGO selects first question by sequence_index
DJANGO converts internal codes to A/B/C letters
            │
            ├──────────────────────────────────────────────────────┐
            │  [AI Wording — once per question]                    │
            │  DJANGO sends question text + A/B/C options + rules  │
            │  → FLOWISE AI writes conversational question text    │
            │  → DJANGO validates:                                 │
            │       no internal variable names exposed             │
            │       no option codes exposed                        │
            │       question text present                          │
            │       all A/B/C letters present                      │
            │       instruction line present (repaired if missing) │
            │  → if valid: show AI version                         │
            │  → if invalid: show deterministic fallback           │
            │  → result cached for 24 hours                        │
            └──────────────────────────────────────────────────────┘
            │
            ▼
Respondent sees question and submits answer (e.g. "A" or "A, C" or "42")
            │
            ▼
DJANGO validates answer
→ splits input, normalises to uppercase
→ checks each letter exists in this question's option set
→ checks single vs multi-choice rules
→ checks exclusive option rules
→ converts letters to internal codes: A → {AIDHH: "1"}
            │
            ▼
DJANGO evaluates routing conditions
→ evaluates every outgoing conditional edge against all answers collected so far
→ first true condition wins: go to that question
→ no true condition: scan forward by sequence, skip conditionally gated questions
→ no eligible question found: interview is complete
            │
            ▼
DJANGO saves InterviewSimulatorTurn to database
(question shown, answer given, codes stored, routing decision, graph highlight)
DJANGO updates InterviewSimulatorSession
(current question advances, answers accumulate, routing trace appended)
            │
            ▼
Repeat until no next question → interview complete
```
