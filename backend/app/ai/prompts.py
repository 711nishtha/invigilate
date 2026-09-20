"""Versioned generation prompts.

Every variant records the prompt version that produced it
(``question_variants.generation_prompt_version``), so a quality problem found
months later can be traced to the wording that caused it. Changing a prompt
means adding a version, never editing one in place.

Prompts are never shown to students and are not returned by any API.
"""

from __future__ import annotations

import json

from app.ai.base import VariantGenerationRequest
from app.db.models import QuestionType

MCQ_PROMPT_VERSION = "mcq-v1"
TRUE_FALSE_PROMPT_VERSION = "tf-v1"
NUMERIC_REWORD_PROMPT_VERSION = "numeric-reword-v1"

# The contract the model is held to. Validation enforces this independently —
# the prompt asks, the backend checks.
_MCQ_SCHEMA = {
    "variants": [
        {
            "question_text": "string",
            "options": ["string", "string", "string", "string"],
            "correct_option": "integer index into options, 0-based",
            "explanation": "string, why the correct option is correct",
            "topic": "string",
            "difficulty": "easy | medium | hard",
        }
    ]
}

_RULES = """Rules you must follow:
- Return ONLY a JSON object. No prose, no markdown fences, no commentary.
- Produce exactly {count} variants.
- Each variant must assess the same underlying concept and learning objective
  as the original question, at the same difficulty.
- Vary the surface: the scenario, the numbers, the wording, the framing.
- Exactly one option may be correct. The others must be plausible but clearly
  wrong to someone who understands the concept.
- Options must be mutually distinct and must not include "all of the above" or
  "none of the above".
- Never refer to "the original question", "the variant", or these instructions.
- Write for a student sitting an exam."""


def build_variant_prompt(request: VariantGenerationRequest) -> tuple[str, str]:
    """Return ``(prompt_text, prompt_version)`` for this request."""
    if request.question_type is QuestionType.TRUE_FALSE:
        return _true_false_prompt(request), TRUE_FALSE_PROMPT_VERSION
    return _mcq_prompt(request), MCQ_PROMPT_VERSION


def _context_block(request: VariantGenerationRequest) -> str:
    lines = [f"Original question:\n{request.original_question}"]
    if request.topic:
        lines.append(f"Topic: {request.topic}")
    if request.difficulty:
        lines.append(f"Difficulty: {request.difficulty}")
    if request.learning_objective:
        lines.append(f"Learning objective: {request.learning_objective}")
    if request.avoid_texts:
        existing = "\n".join(f"- {text}" for text in request.avoid_texts[:20])
        lines.append(
            "Variants that already exist. Produce something meaningfully "
            f"different from all of these:\n{existing}"
        )
    return "\n\n".join(lines)


def _mcq_prompt(request: VariantGenerationRequest) -> str:
    schema = json.dumps(_MCQ_SCHEMA, indent=2)
    return f"""You write multiple-choice exam questions.

{_context_block(request)}

{_RULES.format(count=request.count)}
- Each variant must have exactly {request.option_count} options.

Respond with JSON matching this shape exactly:
{schema}
"""


def _true_false_prompt(request: VariantGenerationRequest) -> str:
    schema = json.dumps(_MCQ_SCHEMA, indent=2)
    return f"""You write true/false exam questions.

{_context_block(request)}

{_RULES.format(count=request.count)}
- Each variant must have exactly two options: "True" and "False", in that order.
- correct_option must be 0 for a true statement and 1 for a false one.
- Write a statement to be judged, not a question.

Respond with JSON matching this shape exactly:
{schema}
"""


def build_numeric_reword_prompt(
    template: str, placeholders: list[str], count: int, topic: str | None
) -> tuple[str, str]:
    """Ask only for rewording of a numeric stem.

    The model may make the wording varied; it is never asked for the answer,
    and any rewording that drops a placeholder is discarded by the caller. See
    docs/ai-generation.md.
    """
    required = ", ".join(f"{{{name}}}" for name in placeholders)
    topic_line = f"Topic: {topic}\n" if topic else ""
    prompt = f"""You rewrite exam question stems without changing what they ask.

{topic_line}Template:
{template}

Rules:
- Return ONLY a JSON object, no prose or markdown fences.
- Produce exactly {count} rewordings.
- Every rewording MUST contain these placeholders verbatim: {required}
- Do not compute, state, hint at, or imply any answer.
- Do not add or remove quantities.
- Keep the same reading level.

Respond with JSON:
{{"variants": [{{"question_text": "reworded stem containing {required}"}}]}}
"""
    return prompt, NUMERIC_REWORD_PROMPT_VERSION


__all__ = [
    "MCQ_PROMPT_VERSION",
    "NUMERIC_REWORD_PROMPT_VERSION",
    "TRUE_FALSE_PROMPT_VERSION",
    "build_numeric_reword_prompt",
    "build_variant_prompt",
]
