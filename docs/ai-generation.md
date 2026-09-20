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

`GeminiProvider` is the first implementation. `StubProvider` returns fixtures and
is what the test suite and offline development use — no test ever calls a paid
API. Selection is by `AI_PROVIDER`; nothing outside `app/ai/` imports Gemini.

## 3. Pipeline

```
teacher request
  -> build prompt (versioned)
  -> provider call (timeout, bounded retries)
  -> strict JSON parse
  -> schema validation
  -> quality checks
  -> duplicate detection
  -> persist as PENDING
  -> teacher review
  -> APPROVED
```

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
| Not a near-duplicate of an existing variant | `DUPLICATE_OF_EXISTING` |
| Not a near-duplicate within the same batch | `DUPLICATE_IN_BATCH` |

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

A variant is served to a student only when `validation_status = APPROVED`, and
approval is a human action through `POST /api/variants/{id}/approve`. There is no
configuration flag that auto-approves AI output. `POST /publish` fails if any
family that could be drawn has no approved variant.

## 10. Cost and failure behaviour

Generation is per-family and on demand, so a teacher pays for what they generate.
Provider failures surface as `VARIANT_GENERATION_FAILED` with the reason and
leave the family untouched — the teacher can retry, edit the original, or write
variants by hand. A quiz never becomes unpublishable because an AI call failed.
