# AI question generation

## 1. Rule zero

**The model is not the authority on correctness.** It generates wording. The
backend decides what is true, and a human decides what ships to students.

Generation happens while the teacher is preparing the quiz. Nothing is generated
during an exam: an exam must not depend on a third-party API being up, fast, or
consistent.

## 2. Provider abstraction

```python
# app/ai/base.py
class AIProvider(ABC):
    @abstractmethod
    async def generate_question_variants(
        self, request: VariantGenerationRequest
    ) -> RawVariantBatch: ...
```

`GeminiProvider` (`app/ai/gemini.py`) is the only module in the codebase that
knows Gemini exists. It speaks the REST API over httpx rather than an SDK, which
keeps the dependency surface small and the failure modes explicit.

`StubProvider` returns deterministic fixtures and can be told to fail in each of
the ways a real provider fails. An autouse fixture installs it for the whole
test suite, so no test run can reach a paid API even if configuration drifts.

Selection is `get_ai_provider()` in `app/ai/factory.py`, driven by
`AI_PROVIDER`. Services depend on the abstraction; **nothing outside `app/ai/`
imports Gemini or names a model**.

### Failure taxonomy

Every failure is a typed subclass of `AIProviderError`, so callers can tell
"try again" from "this will never work":

| Error | Cause | Retryable |
|---|---|---|
| `AITimeout` | No response within `AI_REQUEST_TIMEOUT_SECONDS` | yes |
| `AIRateLimited` | HTTP 429, carries `retry_after` | yes |
| `AITransportError` | Network failure or 5xx | yes |
| `AIRequestRejected` | 4xx other than 429: bad key, bad request, content filter | no |
| `AIMalformedResponse` | Answered, but not with the JSON contract | no |

Retries are bounded (3 attempts, 0.5s then 2s backoff, capped at the provider's
own `Retry-After`) and cover transport only. **A schema failure is never retried
into acceptance** — the teacher is shown the rejection and can regenerate.

The service catches all of these and returns a 200 with `error_code` set. The
question family is left untouched, and an already-published quiz is unaffected:
generation is a preparation-time activity that is never on a student's path.

## 3. Pipeline

```
teacher request
  -> build prompt (versioned)
  -> provider call (timeout, bounded retries)
  -> strict JSON parse          app/ai/gemini.py
  -> schema validation          app/ai/validation.py
  -> quality checks             app/ai/validation.py
  -> duplicate detection        app/ai/dedupe.py
  -> persist as VALIDATED       app/services/variant_service.py
  -> teacher review
  -> APPROVED
  -> PUBLISHED (when the quiz publishes; immutable from here)
```

Note the state a survivor lands in: **VALIDATED, not APPROVED**. Passing every
deterministic check means the variant is well-formed, not that a teacher has
agreed to put it in front of students.

A failure at any stage rejects that variant with a recorded reason. Malformed
output is never repaired into something plausible-looking and never silently
accepted.

## 4. Required output schema

```json
{
  "variants": [
    {
      "question_text": "...",
      "options": ["...", "...", "...", "..."],
      "correct_option": 1,
      "explanation": "...",
      "topic": "...",
      "difficulty": "medium"
    }
  ]
}
```

Validation, in order:

| Check | Rejection reason |
|---|---|
| Parses as JSON (after fenced-block stripping) | `INVALID_JSON` |
| Top level has a `variants` array | `SCHEMA_MISMATCH` |
| `question_text` non-empty, >= 10 chars | `EMPTY_QUESTION` |
| MCQ has exactly 4 options | `WRONG_OPTION_COUNT` |
| Options non-empty and mutually distinct (normalized) | `DUPLICATE_OPTIONS` |
| `0 <= correct_option < len(options)` | `CORRECT_OPTION_OUT_OF_RANGE` |
| `topic` present | `MISSING_TOPIC` |
| `difficulty` in {easy, medium, hard} | `MISSING_DIFFICULTY` |
| `explanation` present, >= 10 chars | `MISSING_EXPLANATION` |
| No option is "all/none of the above" | `CATCH_ALL_OPTION` |
| `correct_option` is not a list, and no `correct_options` key | `MULTIPLE_CORRECT_ANSWERS` |
| `correct_option` is an int, not a bool (`True == 1` in Python) | `CORRECT_OPTION_NOT_AN_INDEX` |
| True/false options are exactly `["True", "False"]` | `TRUE_FALSE_OPTIONS_INVALID` |
| Not a near-duplicate of an existing variant | `DUPLICATE_OF_EXISTING` |
| Not a near-duplicate within the same batch | `DUPLICATE_IN_BATCH` |

`CATCH_ALL_OPTION` exists because "all of the above" becomes meaningless once
option order is shuffled per student, which this platform always does.

The model returning 5 variants of which 4 survive is a normal, logged outcome —
`accepted: 4, rejected: 1` — not an error.

## 5. Duplicate detection

Two layers:

1. **Exact** — `content_hash` = SHA-256 of normalized question text plus sorted
   normalized options, with `UNIQUE (family_id, content_hash)` in the database.
   The database refuses exact duplicates even under a race.
2. **Near** — token-set similarity (`difflib.SequenceMatcher` over normalized
   tokens) against existing variants in the family and against earlier variants
   in the same batch. Default threshold 0.92, configurable per family.

Normalization lowercases, strips punctuation and collapses whitespace, so
"What is the time complexity of binary search?" and "what is the time complexity
of binary search" are the same question.

## 6. Numerical questions are computed, not generated

For `NUMERICAL` families the backend, not the model, produces the answer.

```json
{
  "template": "A classifier correctly predicts {correct} out of {total} samples. Calculate accuracy.",
  "parameters": {
    "total": { "type": "int", "min": 50, "max": 500, "step": 10 },
    "correct": { "type": "int", "min": 10, "max": 500, "depends_on": "total", "rule": "lte" }
  },
  "expression": "correct / total * 100",
  "unit": "%",
  "tolerance": 0.01,
  "decimals": 2
}
```

Parameters are drawn deterministically from a seeded PRNG, the expression is
evaluated by a restricted evaluator (a whitelisted AST walk over arithmetic
nodes — no `eval`, no names, no calls beyond an allowlist), and the result is
stored in `numeric_answer_json` as `{value, tolerance, unit}`.

The model may be asked to rephrase the stem for variety. Its rewording is checked
for placeholder preservation and is discarded if it drops a parameter. **The
computed value is never replaced by a model-produced number.** This is why
`generation_status` distinguishes `DETERMINISTIC` from `AI_GENERATED`.

## 7. Prompting

Prompts are versioned (`generation_prompt_version`, e.g. `mcq-v1`) and stored on
every variant, so a later quality problem can be traced to the prompt that caused
it. Requests use a low temperature, a JSON response MIME type where the provider
supports it, an explicit schema in the instructions, and a hard timeout
(`AI_REQUEST_TIMEOUT_SECONDS`). Retries are bounded and only for transport
errors — a schema failure is never retried into acceptance.

The prompt states: preserve the original question's learning objective and
difficulty, vary surface details, keep exactly one unambiguously correct option,
make distractors plausible, and never reference "the original question".

## 8. Pool sizing

The default is 10 families x 5 variants = 50 approved variants. A student's paper
draws **one variant per family**, so the number of distinct papers is
`5^10 ≈ 9.7 million` before option shuffling — which is the point: not four fixed
sets, but a pool.

With `question_count` smaller than the family count, families are also sampled,
which increases the spread further.

## 9. Review is mandatory

A variant is served to a student only when it is **assignable**:
`validation_status` in (`APPROVED`, `PUBLISHED`) **and** `superseded_by_id IS
NULL`. `QuestionVariant.is_assignable` is the single definition, used by both
the publish rules and the selection pool so the two cannot disagree.

Approval is a human action through `POST /api/variants/{id}/approve`. The
transition table in `services/variant_lifecycle.py` has no path from
`GENERATED` to `APPROVED`, and a test asserts that, so no configuration change
or future code path can make approval automatic. `POST /publish` fails if any
family that could be drawn has no assignable variant.

## 10. Live verification

Verified against the real API on 2026-09-24 via `backend/scripts/gemini_smoke.py`:
authentication, model availability, request format, structured JSON output,
parsing, backend validation of the generated content, and error mapping
(a real 401 and a real 503 both mapped correctly, with bounded retries).

Two measured facts that shape configuration:

- **Latency is ~55-70 seconds and is fixed overhead, not per-variant** (1
  variant 62s, 5 variants 68s). `AI_REQUEST_TIMEOUT_SECONDS` is therefore 120;
  the previous 60s default timed out on essentially every real call. Asking for
  more variants in one request is nearly free.
- **`gemini-3.1-flash-lite` was returning HTTP 503 "high demand"** throughout
  the audit, while `gemini-flash-lite-latest` and `gemini-3.5-flash-lite`
  generated successfully. The model is an environment variable; see
  [runbook.md](./runbook.md#6-gemini).

## 11. Cost and failure behaviour

Generation is per-family and on demand, so a teacher pays for what they generate.
Provider failures surface as `VARIANT_GENERATION_FAILED` with the reason and
leave the family untouched — the teacher can retry, edit the original, or write
variants by hand. A quiz never becomes unpublishable because an AI call failed.
