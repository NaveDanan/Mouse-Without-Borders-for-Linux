"""Pure four-computer matrix topology shared by input and UI code."""

from __future__ import annotations

from .config import MAX_MACHINES

DIRECTIONS = ("left", "right", "top", "bottom")


def coordinates(two_row: bool) -> tuple[tuple[int, int], ...]:
    if two_row:
        return tuple((index // 2, index % 2) for index in range(MAX_MACHINES))
    return tuple((0, index) for index in range(MAX_MACHINES))


def neighbour(
    matrix: list[str],
    two_row: bool,
    machine_name: str,
    direction: str,
    *,
    wrap: bool = False,
) -> str:
    """Return the immediately adjacent non-empty computer, or ``""``."""

    if direction not in DIRECTIONS:
        return ""
    try:
        index = next(
            item
            for item, name in enumerate(matrix)
            if name.casefold() == machine_name.casefold()
        )
    except StopIteration:
        return ""
    if not two_row and direction in {"left", "right"}:
        step = -1 if direction == "left" else 1
        candidates = range(index + step, -1 if step < 0 else MAX_MACHINES, step)
        for candidate in candidates:
            if matrix[candidate].strip():
                return matrix[candidate].strip()
        if wrap:
            candidates = (
                range(MAX_MACHINES - 1, index, -1)
                if step < 0
                else range(0, index)
            )
            for candidate in candidates:
                if matrix[candidate].strip():
                    return matrix[candidate].strip()
        return ""
    row, column = coordinates(two_row)[index]
    delta = {
        "left": (0, -1),
        "right": (0, 1),
        "top": (-1, 0),
        "bottom": (1, 0),
    }[direction]
    wanted = (row + delta[0], column + delta[1])
    for candidate, position in enumerate(coordinates(two_row)):
        if position == wanted:
            return matrix[candidate].strip()
    if wrap and two_row:
        wanted = {
            "left": (row, 1),
            "right": (row, 0),
            "top": (1, column),
            "bottom": (0, column),
        }[direction]
        for candidate, position in enumerate(coordinates(True)):
            if position == wanted:
                return matrix[candidate].strip()
    return ""


def direction_to(
    matrix: list[str], two_row: bool, source: str, destination: str
) -> str:
    for direction in DIRECTIONS:
        if neighbour(matrix, two_row, source, direction).casefold() == destination.casefold():
            return direction
    return ""
