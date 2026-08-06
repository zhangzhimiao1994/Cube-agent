from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, DecimalException


class UnsafeExpression(ValueError):
    """Expression is outside the calculator safety subset."""


@dataclass(frozen=True, slots=True)
class CalculatorResult:
    value: Decimal


class Calculator:
    def __init__(
        self,
        *,
        max_expression_length: int = 512,
        max_abs_result: Decimal = Decimal(1000000000000),
    ) -> None:
        self._max_expression_length = max_expression_length
        self._max_abs_result = max_abs_result

    def evaluate(self, expression: str) -> CalculatorResult:
        if (
            type(expression) is not str
            or not expression.strip()
            or len(expression) > self._max_expression_length
        ):
            raise UnsafeExpression("expression is unsafe")
        try:
            tree = ast.parse(expression, mode="eval")
            value = self._eval(tree.body)
        except (SyntaxError, ValueError, TypeError, DecimalException, OverflowError):
            raise UnsafeExpression("expression is unsafe") from None
        if not value.is_finite() or abs(value) > self._max_abs_result:
            raise UnsafeExpression("expression result is unsafe")
        return CalculatorResult(value=value.normalize() if value != value.to_integral() else value)

    def _eval(self, node: ast.AST) -> Decimal:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if right != right.to_integral() or abs(right) > 128:
                    raise UnsafeExpression("expression is unsafe")
                return left ** int(right)
        raise UnsafeExpression("expression is unsafe")
