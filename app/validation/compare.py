"""Result-set comparison for multi-query validation.

Columns are compared positionally (names are ignored). Order matters only when both queries define ORDER BY and
`ordered=True`; otherwise rows are compared as multisets (duplicates count). NULL equals NULL. Numbers are equal
within an absolute or relative tolerance; ints and floats compare numerically; numeric strings are not coerced.
"""
from __future__ import annotations

import math


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def values_equal(a, b, rel_tol: float, abs_tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if _num(a) and _num(b):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
    return str(a) == str(b)


def _sort_key(row, abs_tol: float):
    key = []
    for v in row:
        if v is None:
            key.append((0, ""))
        elif _num(v):
            key.append((1, round(float(v) / max(abs_tol, 1e-12)) * abs_tol))
        else:
            key.append((2, str(v)))
    return key


def compare_results(cols_a: list[str], rows_a: list[list], cols_b: list[str], rows_b: list[list], *,
                    ordered: bool = False, rel_tol: float = 1e-6, abs_tol: float = 0.01) -> tuple[bool, str]:
    if len(cols_a) != len(cols_b):
        return False, f"Different column counts ({len(cols_a)} vs {len(cols_b)})."
    if len(rows_a) != len(rows_b):
        return False, f"Different row counts ({len(rows_a)} vs {len(rows_b)})."
    if not ordered:
        rows_a = sorted(rows_a, key=lambda r: _sort_key(r, abs_tol))
        rows_b = sorted(rows_b, key=lambda r: _sort_key(r, abs_tol))
    for i, (ra, rb) in enumerate(zip(rows_a, rows_b)):
        for j, (va, vb) in enumerate(zip(ra, rb)):
            if not values_equal(va, vb, rel_tol, abs_tol):
                return False, f"Row {i + 1}, column {j + 1} differs: {va!r} vs {vb!r}."
    return True, f"Results match ({len(rows_a)} rows, {'ordered' if ordered else 'order-insensitive'} comparison)."
