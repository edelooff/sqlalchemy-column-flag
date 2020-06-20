from __future__ import annotations

import operator
from collections import deque
from enum import Enum, auto
from typing import Any, Deque, Dict, Iterator, Optional, Set

from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    AsBoolean,
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    ClauseElement,
    Grouping,
    Null,
    UnaryExpression,
)
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import Boolean

OPERATOR_MAP = {
    operators.in_op: lambda left, right: left in right,
    operators.is_: operator.eq,
    operators.isnot: operator.ne,
    operators.istrue: None,
    operators.isfalse: operator.not_,
}


class Expression:
    """Provides runtime Python evaluation of SQLAlchemy expressions.

    A given SQLAlchemy expression is converted into an internal serialized
    format that allows runtime Python execution based on substitute values for
    the columns involved in the expression. The method to use for this is
    `.evaluate()`.

    When `forrce_bool` is True, bare columns and inverted columns (~Column) are
    converted to booleans. In this operating mode, a column value is False when
    it is None (equivalent `IS NULL`) and True otherwise. In this operating
    mode, the given expression itself is also modified with these same semantics
    and stored on the `sql` attribute.
    """

    def __init__(self, expression: ClauseElement, force_bool: bool = False):
        self.sql = rephrase_as_boolean(expression) if force_bool else expression
        self.serialized = tuple(self._serialize(expression, force_bool=force_bool))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.serialized == other.serialized

    def evaluate(self, column_values: Dict[Column, Any]) -> Any:
        """Evaluates the SQLAlchemy expression on the current column values."""
        stack = Stack()
        for itype, arity, value in self.serialized:
            if itype is SymbolType.literal:
                stack.push(value)
            elif itype is SymbolType.column:
                stack.push(column_values[value])
            else:
                stack.push(value(*stack.popn(arity)))
        return stack.pop()

    @property
    def columns(self) -> Set[Column]:
        """Returns a set of columns used in the expression."""
        coltype = SymbolType.column
        return {symbol.value for symbol in self.serialized if symbol.type is coltype}

    def _serialize(
        self, expr: ClauseElement, force_bool: bool = False
    ) -> Iterator[Symbol]:
        """Serializes an SQLAlchemy expression to Python functions.

        This takes an SQLAlchemy expression tree and converts it into an
        equivalent set of Python Symbols. The generated format is that
        of a reverse Polish notation. This allows the expression to be easily
        evaluated with column value substitutions.
        """
        # Simple and direct value types
        if isinstance(expr, BindParameter):
            yield Symbol(expr.value)
        elif isinstance(expr, Grouping):
            value = [element.value for element in expr.element]
            yield Symbol(value)
        elif isinstance(expr, Null):
            yield Symbol(None)
        # Columns and column-wrapping functions
        elif isinstance(expr, Column):
            if force_bool and not isinstance(expr.type, Boolean):
                yield from self._serialize(expr.isnot(None))
            else:
                yield Symbol(expr)
        elif isinstance(expr, AsBoolean):
            yield Symbol(expr.element)
            if (func := OPERATOR_MAP[expr.operator]) is not None:
                yield Symbol(func, arity=1)
        elif isinstance(expr, UnaryExpression):
            target = expr.element
            target_is_column = isinstance(target, Column)
            if force_bool and expr.operator == operator.inv and target_is_column:
                yield from self._serialize(target.is_(None))
            else:
                yield from self._serialize(target, force_bool=force_bool)
                yield Symbol(expr.operator, arity=1)
        # Multi-clause expressions
        elif isinstance(expr, BooleanClauseList):
            for clause in expr.clauses:
                yield from self._serialize(clause, force_bool=force_bool)
            yield Symbol(expr.operator, arity=len(expr.clauses))
        elif isinstance(expr, BinaryExpression):
            if isinstance(expr.operator, operators.custom_op):
                raise TypeError(f"Unsupported operator {expr.operator}")
            yield from self._serialize(expr.right)
            yield from self._serialize(expr.left)
            yield Symbol(OPERATOR_MAP.get(expr.operator, expr.operator), arity=2)
        else:
            expr_type = type(expr).__name__
            raise TypeError(f"Unsupported expression {expr} of type {expr_type}")


class Stack:
    def __init__(self) -> None:
        self._stack: Deque[Any] = deque()

    def push(self, frame: Any) -> None:
        self._stack.append(frame)

    def pop(self) -> Any:
        return self._stack.pop()

    def popn(self, size: int) -> Iterator[Any]:
        return (self._stack.pop() for _ in range(size))


class Symbol:
    __slots__ = "value", "type", "arity"

    def __init__(self, value: Any, arity: Optional[int] = None):
        self.value = value
        self.type = self._determine_type(value)
        self.arity = arity

    def _determine_type(self, value: Any) -> SymbolType:
        if isinstance(value, Column):
            return SymbolType.column
        if callable(value):
            return SymbolType.operator
        return SymbolType.literal

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return tuple(self) == tuple(other)

    def __iter__(self) -> Iterator[Any]:
        yield from (self.type, self.arity, self.value)


class SymbolType(Enum):
    column = auto()
    literal = auto()
    operator = auto()


def rephrase_as_boolean(expr: ClauseElement) -> ClauseElement:
    """Rephrases SQL expression allowing boolean usage of non-bool columns.

    This is done by converting bare non-Boolean columns (those not used in
    a binary expression) in to "IS NOT NULL" clauses, and inversed columns
    (~Column) into the negated form ("IS NULL").
    """
    if isinstance(expr, Column) and not isinstance(expr.type, Boolean):
        return expr.isnot(None)
    elif isinstance(expr, UnaryExpression):
        if expr.operator is operator.inv and isinstance(expr.element, Column):
            return expr.element.is_(None)
        return expr
    elif isinstance(expr, BooleanClauseList):
        expr.clauses = list(map(rephrase_as_boolean, expr.clauses))
        return expr
    return expr
