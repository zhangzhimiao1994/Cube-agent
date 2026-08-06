from __future__ import annotations

from decimal import Decimal

import pytest

from agent_hub.capabilities.tools.calculator import Calculator, UnsafeExpression


def test_calculator_evaluates_safe_arithmetic() -> None:
    calculator = Calculator()

    result = calculator.evaluate("2 + 3 * (4 - 1) / 3")

    assert result.value == Decimal(5)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('whoami')",
        "open('secret.txt').read()",
        "(1).__class__",
        "lambda x: x",
        "[1][0]",
        "a + 1",
    ],
)
def test_calculator_rejects_calls_imports_attributes_and_names(expression: str) -> None:
    calculator = Calculator()

    with pytest.raises(UnsafeExpression):
        calculator.evaluate(expression)


def test_calculator_rejects_long_expressions() -> None:
    calculator = Calculator(max_expression_length=5)

    with pytest.raises(UnsafeExpression):
        calculator.evaluate("1 + 2 + 3")


def test_calculator_rejects_results_above_bound() -> None:
    calculator = Calculator(max_abs_result=Decimal(100))

    with pytest.raises(UnsafeExpression):
        calculator.evaluate("10 ** 3")
