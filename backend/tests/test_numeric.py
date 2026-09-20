"""Deterministic numeric generation.

The rule under test: for numeric questions the backend computes the answer. A
model may reword the stem, but no model-produced number is ever stored.
"""

from __future__ import annotations

import pytest

from app.ai.numeric import (
    NumericTemplateError,
    evaluate_expression,
    generate_numeric_variants,
    placeholders_in,
    reword_preserves_placeholders,
)

ACCURACY_CONFIG = {
    "template": "A classifier correctly predicts {correct} out of {total} samples. Calculate accuracy.",
    "parameters": {
        "total": {"type": "int", "min": 50, "max": 500, "step": 10},
        "correct": {"type": "int", "min": 10, "max": 500, "depends_on": "total", "rule": "lte"},
    },
    "expression": "correct / total * 100",
    "unit": "%",
    "tolerance": 0.01,
    "decimals": 2,
}


class TestSafeExpressionEvaluation:
    def test_basic_arithmetic(self) -> None:
        assert evaluate_expression("a + b * 2", {"a": 1, "b": 3}) == 7

    def test_whitelisted_functions(self) -> None:
        assert evaluate_expression("sqrt(x)", {"x": 16}) == 4
        assert evaluate_expression("round(x)", {"x": 2.6}) == 3
        assert evaluate_expression("max(a, b)", {"a": 1, "b": 9}) == 9

    def test_percentage_expression(self) -> None:
        assert evaluate_expression("correct / total * 100", {"correct": 45, "total": 60}) == 75.0

    @pytest.mark.parametrize(
        "hostile",
        [
            "__import__('os').system('rm -rf /')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__[0]",
            "eval('1+1')",
            "exec('x=1')",
            "globals()",
            "x.__class__",
            "[i for i in range(10)]",
            "lambda: 1",
        ],
    )
    def test_code_execution_is_refused(self, hostile: str) -> None:
        """A teacher-authored expression is input, and input is not executed."""
        with pytest.raises(NumericTemplateError):
            evaluate_expression(hostile, {"x": 1})

    def test_an_undeclared_name_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            evaluate_expression("a + undeclared", {"a": 1})

    def test_division_by_zero_is_a_template_error_not_a_crash(self) -> None:
        with pytest.raises(NumericTemplateError):
            evaluate_expression("a / b", {"a": 1, "b": 0})

    def test_a_huge_exponent_is_refused(self) -> None:
        """Guards against a template exhausting memory."""
        with pytest.raises(NumericTemplateError):
            evaluate_expression("9 ** x", {"x": 10_000})

    def test_syntactically_invalid_expression_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            evaluate_expression("a +* b", {"a": 1, "b": 2})

    def test_a_non_finite_result_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            evaluate_expression("exp(x)", {"x": 100000})


class TestDeterminism:
    def test_the_same_seed_produces_the_same_questions(self) -> None:
        first = generate_numeric_variants(ACCURACY_CONFIG, count=5, seed=12345)
        second = generate_numeric_variants(ACCURACY_CONFIG, count=5, seed=12345)
        assert [v.question_text for v in first] == [v.question_text for v in second]
        assert [v.value for v in first] == [v.value for v in second]

    def test_different_seeds_produce_different_questions(self) -> None:
        first = generate_numeric_variants(ACCURACY_CONFIG, count=5, seed=1)
        second = generate_numeric_variants(ACCURACY_CONFIG, count=5, seed=2)
        assert [v.question_text for v in first] != [v.question_text for v in second]

    def test_generation_does_not_use_the_global_rng(self) -> None:
        """Otherwise a paper could not be re-derived from its stored seed."""
        import random

        random.seed(999)
        first = generate_numeric_variants(ACCURACY_CONFIG, count=3, seed=42)
        random.seed(111)
        second = generate_numeric_variants(ACCURACY_CONFIG, count=3, seed=42)
        assert [v.value for v in first] == [v.value for v in second]


class TestBackendComputesTheAnswer:
    def test_the_answer_matches_the_expression(self) -> None:
        for variant in generate_numeric_variants(ACCURACY_CONFIG, count=10, seed=7):
            expected = variant.parameters["correct"] / variant.parameters["total"] * 100
            assert abs(variant.value - expected) < 0.01

    def test_dependency_rules_are_respected(self) -> None:
        """correct <= total, so accuracy can never exceed 100%."""
        for variant in generate_numeric_variants(ACCURACY_CONFIG, count=15, seed=3):
            assert variant.parameters["correct"] <= variant.parameters["total"]
            assert variant.value <= 100.0

    def test_parameters_are_stored_for_recomputation(self) -> None:
        """A disputed mark must be explainable from the stored inputs."""
        variant = generate_numeric_variants(ACCURACY_CONFIG, count=1, seed=5)[0]
        answer = variant.numeric_answer_json()
        assert answer["parameters"]["total"] > 0
        assert answer["unit"] == "%"
        assert answer["tolerance"] == 0.01

    def test_the_stem_contains_the_substituted_values(self) -> None:
        variant = generate_numeric_variants(ACCURACY_CONFIG, count=1, seed=11)[0]
        assert "{" not in variant.question_text
        assert str(int(variant.parameters["total"])) in variant.question_text

    def test_step_is_honoured(self) -> None:
        for variant in generate_numeric_variants(ACCURACY_CONFIG, count=10, seed=21):
            assert variant.parameters["total"] % 10 == 0

    def test_variants_are_distinct(self) -> None:
        variants = generate_numeric_variants(ACCURACY_CONFIG, count=8, seed=77)
        assert len({v.content_hash for v in variants}) == len(variants)


class TestTemplateErrors:
    def test_a_missing_template_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants({"expression": "a", "parameters": {"a": {}}}, count=1, seed=1)

    def test_a_missing_expression_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants({"template": "{a}", "parameters": {"a": {}}}, count=1, seed=1)

    def test_a_parameter_without_bounds_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants(
                {"template": "{a}", "expression": "a", "parameters": {"a": {"type": "int"}}},
                count=1,
                seed=1,
            )

    def test_min_greater_than_max_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants(
                {
                    "template": "{a}",
                    "expression": "a",
                    "parameters": {"a": {"type": "int", "min": 10, "max": 1}},
                },
                count=1,
                seed=1,
            )

    def test_an_unsatisfiable_dependency_is_refused_not_hung(self) -> None:
        """A rule nothing can satisfy must fail, not loop forever."""
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants(
                {
                    "template": "{a} {b}",
                    "expression": "a + b",
                    "parameters": {
                        "a": {"type": "int", "min": 1, "max": 1},
                        "b": {"type": "int", "min": 5, "max": 5, "depends_on": "a", "rule": "lt"},
                    },
                },
                count=1,
                seed=1,
            )

    def test_a_template_referencing_an_undeclared_parameter_is_refused(self) -> None:
        with pytest.raises(NumericTemplateError):
            generate_numeric_variants(
                {
                    "template": "{a} and {missing}",
                    "expression": "a",
                    "parameters": {"a": {"type": "int", "min": 1, "max": 5}},
                },
                count=1,
                seed=1,
            )


class TestRewordGuard:
    def test_placeholders_are_extracted(self) -> None:
        assert set(placeholders_in("A of {correct} out of {total}")) == {"correct", "total"}

    def test_a_reword_keeping_placeholders_is_accepted(self) -> None:
        assert reword_preserves_placeholders(
            "Out of {total} samples, {correct} were right. Accuracy?", ["correct", "total"]
        )

    def test_a_reword_dropping_a_placeholder_is_rejected(self) -> None:
        """Otherwise the question loses the quantity it asks about."""
        assert not reword_preserves_placeholders(
            "Out of many samples, {correct} were right.", ["correct", "total"]
        )
