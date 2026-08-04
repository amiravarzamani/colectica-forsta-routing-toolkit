# How Routing, Extraction, Validation & AI Work
## A Manager-Level Explainer

---

## The Big Picture First

When a survey JSON file is uploaded, the system does four completely separate jobs:

1. **Extract** — read the JSON and understand its structure
2. **Route** — during a live simulation, decide which question comes next
3. **Validate** — check that a respondent's answer is acceptable
4. **Present** — format the question into natural language for the respondent

**Jobs 1, 2, and 3 are done entirely by Django (our application code) with zero AI involvement.**
**Job 4 is the only place AI is used — and even there, Django checks the AI's output and has a backup.**

---

## Part 1 — What Happens When a JSON File Is Imported

A Colectica survey file is a large, deeply nested JSON document. It describes hundreds of
questions, answer options, and branching rules all tangled together in a tree structure.

The system reads this file in two passes:

---

### Pass 1: Schema Extraction (finding the questions)

**File:** `services/schema_extractor.py` — class `ColecticaSchemaExtractor`

The extractor walks every node in the JSON tree. Whenever it finds an object that contains
a `Question` key, it reads:

- `name` — the internal variable name (e.g. `AIDHH`, `BENBASE`, `FRVAL`)
- `text` — the actual question wording shown to a respondent
- `question_type` — detected from the `ResponseDomains` field:
  - Does it have `Codes`? → single_choice or multi_choice
  - Does it have `NumericType`? → number
  - Does it have `RepresentationType = 2`? → date
  - Otherwise? → text or unknown
- `options` — each answer option's code (the internal value), label (the visible text),
  whether it is a "missing" value (Don't know / Refused), and whether it is exclusive
  (cannot be combined with other answers)

Every question found is saved to the database as a `NormalizedQuestion` record.
The word "normalized" means the system has converted the raw Colectica format into a
clean, consistent structure our application can work with.

**Example of what gets extracted from JSON:**

```
Raw Colectica JSON → NormalizedQuestion record:
  name:          "AIDHH"
  text:          "Does anyone in your household provide regular care or support
                  to a family member, friend or neighbour?"
  question_type: "single_choice"
  options:       [ {code: "1", label: "Yes"},
                   {code: "2", label: "No"} ]
  sequence_index: 1
```

---

### Pass 2: Routing Extraction (finding the rules)

**File:** `services/routing_extractor.py` — class `ColecticaRoutingExtractor`

After questions are extracted, the system does a second walk of the same JSON looking
for routing rules. It finds three kinds:

#### A) Conditional edges
These are "if–then" rules. In Colectica they appear as `Branches` objects with a
`SourceCodeExpressions` field that contains the condition text.

```
Example from the JSON:
  condition: "if [AIDHH = 1]"
  target:    "NAIDHH"

Meaning: If the respondent answered AIDHH with code 1 (Yes),
         then show question NAIDHH next.
```

The system extracts the source variable from the condition text using pattern matching
(it reads identifiers like `AIDHH` and checks if they are known question names).
It saves a `RoutingEdge` record:

```
source_question:  "AIDHH"
target_question:  "NAIDHH"
condition_text:   "if [AIDHH = 1]"
edge_type:        "conditional"
```

#### B) Loop edges
Some survey modules repeat a block of questions for each member of a household.
The extractor detects activity objects whose name contains the word "loop" and records
which questions belong inside that loop.

#### C) Sequential edges
The simplest rule: question N is followed by question N+1 in sequence order.
These are recorded as a fallback so the system always knows the default order.

---

## Part 2 — How Routing Works During a Live Interview Simulation

**File:** `services/interview_router.py` — function `_choose_next_question`

When a respondent answers a question, the system must decide which question to show next.
This decision is made **entirely by our application code** using a strict priority order:

```
STEP A: Look at all outgoing routing edges from the current question.

STEP B: For each conditional edge, evaluate the condition against
        the answers collected so far.

STEP C: If any conditional edge evaluates to TRUE → go to that question.
        (First true edge wins, in sequence order.)

STEP D: If no conditional edge is true → fall back to sequential order.
        Scan forward through questions by sequence_index.
        But skip any question whose incoming conditional gate is not satisfied.

STEP E: If no next question is found → the interview is complete.
```

### How Conditions Are Evaluated

**File:** `services/condition_evaluator.py`

The condition text extracted from the JSON (e.g. `if [AIDHH = 1]`) is parsed
by a purpose-built evaluator that supports:

| Pattern | Example |
|---|---|
| Equality | `AIDHH = 1` |
| Not equal | `AIDHH <> 2` |
| Greater than | `NAIDHH > 1` |
| List membership | `BENBASE IN 1,2,3` |
| Pipe-separated list | `FRJT = 2\|3\|4` |
| AND combinations | `AIDHH = 1 AND NAIDHH > 0` |
| OR combinations | `BENBASE = 1 OR BENBASE = 2` |
| External variables | `HHGRID.HHSIZE > 1` |

The evaluator always returns one of four statuses:
- **true** — condition is satisfied by current answers
- **false** — condition is not satisfied
- **unknown** — a required answer has not been collected yet
- **unsupported** — the expression is too complex to parse safely

This is fully deterministic — the same answers always produce the same routing decision.
No randomness, no AI.

### The Eligibility Check (skipping questions)

When falling back to sequential order, the system does not blindly go to the next question.
It checks: does this question have any incoming conditional gates?
If yes, at least one of them must evaluate to true before the question is shown.
Otherwise it is skipped and the scan continues to the next one.

This ensures that conditionally-gated questions are never shown to respondents who
do not meet the criteria, even during sequential fallback.

---

## Part 3 — What Is "Deterministic Fallback" and Why Does It Exist?

### The term "deterministic" explained

Something is **deterministic** when, given the same inputs, it always produces the
exact same output — no randomness, no inference, no guessing.

Our routing engine is deterministic: the same set of answers always leads to the
same next question. You could run the same interview a thousand times with the same
answers and always get identical results.

### Where the word "fallback" comes in

The only place the system uses AI is when **formatting the question wording** for
the respondent (making it sound natural and conversational). This is done by a
Flowise AI agentflow.

But AI systems can fail. The Flowise server might be:
- Temporarily down
- Returning a garbled response
- Accidentally exposing internal variable names (like `AIDHH`) to the respondent

So the system has a **fallback**: a simple, locally-computed function that formats
the question without any AI at all:

```python
def build_fallback_assistant_message(question):
    # Just lay out the question text and lettered options. No AI needed.

    "Does anyone in your household provide regular care..."

    A. Yes
    B. No

    Choose one option only. Answer using one letter only, for example: A.
```

This is called the "deterministic fallback" because:
- It is **deterministic** (same question → always the same formatted output)
- It is a **fallback** (only used when the AI path fails)

**The interview simulation never stops working because of an AI failure.**
The question still appears correctly. The routing still works perfectly.
The respondent's answers are still recorded. Only the conversational polish is missing.

---

## Part 4 — What Is the AI Actually Doing Here?

The AI (Flowise) has **two and only two jobs** in this system.
It has no authority over anything else.

---

### AI Job 1: Module Review (advisory analysis)

**When:** After a designer has uploaded and processed a survey module,
they can optionally request an AI review.

**What Django sends to the AI:**
- The list of all question names and types
- All conditional routing edges (indexed)
- Up to 10 test cases (called "coverage intents") that the system has
  generated deterministically to exercise each conditional branch

**What the AI is asked to do:**
Review this structure and identify:
- Schema problems (missing labels, unusual question types)
- Routing risks (unreachable branches, complex conditions)
- Gaps in test coverage

**What Django does with the AI's response:**
Django does NOT trust it blindly. It checks every factual claim the AI makes:

| What Django checks | Why |
|---|---|
| Did the AI echo back the exact question names we sent? | Detect hallucinated names |
| Did the AI echo back the exact external variable list? | Detect invented dependencies |
| Did the AI confirm the correct conditional edge count? | Detect invented edges |
| Did the AI copy the covered/uncovered edge indexes exactly? | Detect wrong coverage claims |
| Did the AI use any forbidden keys we told it not to? | Detect overreach |

If any check fails → the review is marked **failed** and the designer is told.
If all checks pass → the review is marked **passed** and saved.

**The AI's review is advisory only.** It cannot change the routing logic, the question
structure, or the test cases. It can only add observations and suggestions that the
designer chooses to act on.

---

### AI Job 2: Interview Wording (natural language formatting)

**When:** Every time a question is about to be shown to a respondent during simulation.

**What Django sends to the AI:**
- The question text (as extracted from the survey)
- The answer options with A/B/C letters (never the internal codes like "1", "2", "3")
- The module name and version
- Previous answers (for conversational context)
- A strict list of rules the AI must follow

**What the AI is asked to do:**
Present the question in a natural, friendly, conversational way —
the way a human interviewer would read it to a respondent.

**What Django does with the AI's response:**
Before showing anything to the respondent, Django runs a validation pipeline:

```
1. Does the response contain the internal variable name (e.g. "AIDHH")?
   → YES: REJECTED. Use fallback.  (Respondents must never see variable names.)

2. Does the response expose option codes (e.g. "code 1", "option code: 3")?
   → YES: REJECTED. Use fallback.  (Respondents must never see internal codes.)

3. Does the response contain the actual question text?
   → NO:  REJECTED. Use fallback.  (AI must not invent its own questions.)

4. Does the response show all the A/B/C option letters?
   → NO:  REJECTED. Use fallback.  (AI must not drop or invent options.)

5. Is only the final instruction line missing?
   → YES: REPAIRED. Django appends the correct instruction line.
           ("Choose one option only. Answer using one letter only, for example: A.")

6. All checks pass → show the AI-formatted question to the respondent.
```

**Caching:** To avoid calling the AI every time the same question appears
(which would be slow and expensive), the system caches each AI-formatted question
for 24 hours. The cache key is a fingerprint (SHA-256 hash) of the question content.
If the same question appears again within 24 hours, the cached version is used instantly.

---

## Summary: Who Controls What

| Concern | Controlled by | AI involved? |
|---|---|---|
| Parsing the survey JSON | Django (deterministic code) | No |
| Storing questions and routing rules | Django (database) | No |
| Deciding which question comes next | Django (condition evaluator + router) | No |
| Validating respondent answers | Django (answer validation) | No |
| Generating test cases for routing branches | Django (coverage intent builder) | No |
| Formatting question wording for respondents | Flowise AI (with Django validation + fallback) | Yes |
| Reviewing schema and routing quality | Flowise AI (advisory, post-validated by Django) | Yes |

---

## One-Sentence Version for Executives

> Our application code reads the survey file, controls all routing logic, and validates
> all answers with complete certainty; the AI is only used to make questions sound
> natural to respondents and to give advisory feedback on survey quality —
> and if the AI ever fails, the system continues working perfectly without it.
