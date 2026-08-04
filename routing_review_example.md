# How the AI Routing Review Actually Works — A Concrete Example

---

## The Scenario

Imagine a survey module called `benefits_w18` with 5 questions about household caring
and benefits. The designer has already uploaded the JSON, extracted the schema,
extracted the routing, and built the graph. Now they click "Run AI Review".

Here is what the 5 questions look like after Django has extracted them:

| Name | Type | What it asks |
|---|---|---|
| AIDHH | single_choice | Does anyone in your household provide regular care to someone? (Yes/No) |
| NAIDHH | number | How many people in your household provide care? |
| BENBASE | multi_choice | Which benefits does your household currently receive? |
| FICODE | single_choice | What is the main source of family income? |
| FRVAL | number | What is the value of that income per week? |

And Django has extracted 3 conditional routing edges:

| Index | Source | Condition | Target |
|---|---|---|---|
| 0 | AIDHH | if [AIDHH = 1] | NAIDHH |
| 1 | NAIDHH | if [NAIDHH > 1] | BENBASE |
| 2 | HHGRID.HHSIZE | if [(HHGRID.HHSIZE - Number of absent hhold members) > 1] | FICODE |

Notice that edge 2 uses `HHGRID.HHSIZE` — this is NOT a question in this module.
It comes from a different module (the household grid). Django detects this automatically
and calls it an "external variable".

---

## Step 1 — What Django Sends to the AI

Django does NOT send the raw Colectica JSON to the AI.
It builds a small, clean, structured payload from its own database records.
This is what the AI actually receives:

```json
{
  "payload_type": "flowise_review",

  "module": {
    "name": "benefits_w18",
    "version": "v02"
  },

  "schema_summary": {
    "question_count": 5,
    "question_names": ["AIDHH", "NAIDHH", "BENBASE", "FICODE", "FRVAL"],
    "question_types": {
      "AIDHH":   "single_choice",
      "NAIDHH":  "number",
      "BENBASE": "multi_choice",
      "FICODE":  "single_choice",
      "FRVAL":   "number"
    }
  },

  "routing_summary": {
    "conditional_edge_count": 3,
    "external_variables": ["HHGRID.HHSIZE"],
    "conditional_edges": [
      {
        "index": 0,
        "source_question": "AIDHH",
        "target_question": "NAIDHH",
        "condition_text":  "if [AIDHH = 1]"
      },
      {
        "index": 1,
        "source_question": "NAIDHH",
        "target_question": "BENBASE",
        "condition_text":  "if [NAIDHH > 1]"
      },
      {
        "index": 2,
        "source_question": "HHGRID.HHSIZE",
        "target_question": "FICODE",
        "condition_text":  "if [(HHGRID.HHSIZE - Number of absent hhold members) > 1]"
      }
    ]
  },

  "coverage_intents": {
    "intent_count": 3,
    "covered_conditional_edges":           [0, 1, 2],
    "possibly_uncovered_conditional_edges": [],
    "intents": [
      {
        "name":             "Trigger NAIDHH",
        "covers_edges":     [0],
        "source_question":  "AIDHH",
        "target_question":  "NAIDHH",
        "condition_text":   "if [AIDHH = 1]",
        "seed_answers":     {"AIDHH": "1"},
        "required_external_context": {}
      },
      {
        "name":             "Trigger BENBASE",
        "covers_edges":     [1],
        "source_question":  "NAIDHH",
        "target_question":  "BENBASE",
        "condition_text":   "if [NAIDHH > 1]",
        "seed_answers":     {"AIDHH": "1", "NAIDHH": 2},
        "required_external_context": {}
      },
      {
        "name":             "Trigger FICODE",
        "covers_edges":     [2],
        "source_question":  "HHGRID.HHSIZE",
        "target_question":  "FICODE",
        "condition_text":   "if [(HHGRID.HHSIZE - Number of absent hhold members) > 1]",
        "seed_answers":     {},
        "required_external_context": {"HHGRID.HHSIZE": 2},
        "notes": [
          "Condition contains derived household expression. Suggested HHGRID.HHSIZE value assumes absent household members is 0."
        ]
      }
    ]
  },

  "instructions": {
    "role": "advisory_review_only",
    "rules": [
      "Review coverage_intents only.",
      "Do not create new test cases.",
      "Copy coverage_intents.covered_conditional_edges exactly.",
      "Copy coverage_intents.possibly_uncovered_conditional_edges exactly.",
      "Do not produce expected_active_questions.",
      "Do not produce expected_skipped_questions."
    ]
  }
}
```

This is the complete picture the AI is given. Nothing more.
The AI does not see the raw Colectica JSON.
The AI does not see the full option lists.
The AI does not see any respondent data.

---

## Step 2 — What the AI Actually Does With This

The AI reads the payload and produces a written review. It is essentially
reading a structured description of the survey and asking itself:

**On the schema:**
- "I see 5 questions. NAIDHH is a number question — does it need any range validation?
  The system has not flagged any constraints on it."
- "BENBASE is multi-choice — are respondents expected to select all that apply?
  Worth flagging for the designer to confirm."

**On the routing:**
- "Edge 0: AIDHH = 1 → show NAIDHH. This means if the respondent answers No to
  AIDHH, they skip NAIDHH entirely. That seems intentional — only those who have
  carers in the household need to say how many."
- "Edge 1: NAIDHH > 1 → show BENBASE. This means BENBASE is only shown if
  there is more than one carer. What happens if NAIDHH = 1? Is BENBASE skipped?
  If so, is that intentional?"
- "Edge 2: depends on HHGRID.HHSIZE which is an external variable. This means
  this module assumes HHGRID.HHSIZE has already been collected in a previous module.
  If this module is ever run in isolation, edge 2 cannot be evaluated."

**On the coverage intents:**
- "I see 3 intents covering edges 0, 1, and 2. All 3 conditional edges appear
  to be covered. However, intent 3 relies on an external context value for
  HHGRID.HHSIZE. This is an approximation — the actual household size at runtime
  may produce a different result."

The AI then writes all of this as a structured JSON response like this:

```json
{
  "schema_review": {
    "real_question_names": ["AIDHH", "NAIDHH", "BENBASE", "FICODE", "FRVAL"],
    "observations": [
      "NAIDHH is a number question. No range constraints are visible in the schema — designers should confirm whether negative or very large numbers are valid.",
      "BENBASE is multi_choice — designers should confirm that respondents are expected to select all that apply."
    ],
    "warnings": []
  },

  "routing_review": {
    "conditional_edge_count": 3,
    "external_variables": ["HHGRID.HHSIZE"],
    "observations": [
      "Edge 0 (AIDHH=1 → NAIDHH): Respondents who answer No to AIDHH skip NAIDHH. This appears intentional.",
      "Edge 1 (NAIDHH>1 → BENBASE): BENBASE is only shown when more than one carer exists. If NAIDHH=1 this branch is not triggered. Designers should confirm whether single-carer households should also see BENBASE.",
      "Edge 2 depends on external variable HHGRID.HHSIZE. If this module runs without prior household grid data, this edge cannot be evaluated and FICODE may never be shown."
    ],
    "risks": [
      "External dependency on HHGRID.HHSIZE creates a runtime risk if module sequencing is changed."
    ]
  },

  "coverage_intent_review": {
    "intent_count": 3,
    "covered_conditional_edges":           [0, 1, 2],
    "possibly_uncovered_conditional_edges": [],
    "observations": [
      "All 3 conditional edges have a coverage intent.",
      "Intent for edge 2 relies on HHGRID.HHSIZE=2 as an approximation. Actual test runs should supply real household data."
    ]
  },

  "suggested_improvements": [
    {
      "type": "routing_risk",
      "message": "Consider adding a coverage intent for the case where NAIDHH=1 to confirm that BENBASE is correctly skipped.",
      "related_edge_indexes": [1]
    },
    {
      "type": "external_dependency",
      "message": "Document that HHGRID.HHSIZE must be collected before this module runs.",
      "related_edge_indexes": [2]
    }
  ]
}
```

---

## Step 3 — What Django Does With the AI's Response

Django does NOT trust the AI's response automatically.
It runs a validation pass comparing what the AI wrote against what it knows from
its own database. This is where the word "advisory" becomes important — the AI
gave its opinion, now Django checks if the AI was accurate.

Django checks these specific fields one by one:

### Check 1: Question names
Django compares:
- What Django knows: `["AIDHH", "NAIDHH", "BENBASE", "FICODE", "FRVAL"]`
- What the AI wrote in `schema_review.real_question_names`: `["AIDHH", "NAIDHH", "BENBASE", "FICODE", "FRVAL"]`
- Result: **match → pass**

If the AI had written `["AIDHH", "NAIDHH", "BENBASE", "FICODE", "FRVAL", "AIDXHH"]`
(inventing a 6th question name that doesn't exist), Django would catch it → **fail**.

### Check 2: Conditional edge count
Django compares:
- What Django knows: 3
- What the AI wrote in `routing_review.conditional_edge_count`: 3
- Result: **match → pass**

### Check 3: External variables
Django compares:
- What Django knows: `["HHGRID.HHSIZE"]`
- What the AI wrote in `routing_review.external_variables`: `["HHGRID.HHSIZE"]`
- Result: **match → pass**

### Check 4: Covered edge indexes
Django compares:
- What Django knows: `[0, 1, 2]`
- What the AI wrote in `coverage_intent_review.covered_conditional_edges`: `[0, 1, 2]`
- Result: **match → pass**

### Check 5: Uncovered edge indexes
Django compares:
- What Django knows: `[]` (none uncovered)
- What the AI wrote in `coverage_intent_review.possibly_uncovered_conditional_edges`: `[]`
- Result: **match → pass**

### Check 6: Forbidden keys
Django scans the entire response for the keys `expected_active_questions`
and `expected_skipped_questions`. These are forbidden because only Django's
deterministic simulator is allowed to determine which questions are active or skipped.
If the AI had tried to predict those, Django would reject the entire review.
- Result: neither key found → **pass**

---

## Step 4 — The Outcome

All 6 checks passed. Django saves the review with status **passed**.

The designer can now read the AI's observations:
- The risk about NAIDHH=1 not triggering BENBASE
- The external dependency warning about HHGRID.HHSIZE
- The suggestion to add a test case for the single-carer path

These are observations and suggestions. The designer decides what to do with them.
The routing logic in the database has not changed. The questions have not changed.
The coverage intents have not changed. The AI only wrote a commentary.

---

## What "Advisory" Actually Means in Plain Language

Think of it like hiring an auditor to review a financial report.

- The accountant (Django) prepares the numbers from source records.
- The auditor (Flowise AI) reads the report and writes observations and risks.
- The auditor is not allowed to change the numbers.
- The company (Django again) then checks whether the auditor correctly
  read the numbers back — if the auditor wrote down a different total
  than what was on the report, their review is rejected.
- If the auditor's observations are coherent and accurate, the report is
  marked reviewed. The management team then decides what to act on.

That is exactly what happens here.
Django prepares the data. The AI reviews it. Django validates the review.
The designer reads the advisory output and uses their own judgement.

---

## What Would Cause a Review to FAIL

Here are concrete examples of AI responses that Django would reject:

| AI does this | Why Django rejects it |
|---|---|
| Lists a question name like `AIDXHH` that is not in the module | Hallucinated question name |
| Reports `conditional_edge_count: 4` when there are only 3 | Wrong count |
| Says `covered_conditional_edges: [0, 1]` when Django says `[0, 1, 2]` | Wrong coverage claim |
| Includes a key `expected_active_questions: ["AIDHH", "NAIDHH"]` | Forbidden — only Django decides active questions |
| Reports `external_variables: ["HHGRID.HHSIZE", "DEMOGRAPHICS.JBSTAT"]` when only `HHGRID.HHSIZE` exists | Invented external dependency |

Any one of these causes the entire review to be saved as `failed`,
and the errors are shown to the designer so they know what went wrong.
