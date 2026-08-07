"""Real display geometry used to place portal pointer barriers.

The visible matrix models computers. Physical monitors are an internal detail:
the compositor geometry determines whether a requested hand-off edge is on the
outer boundary of the Linux desktop. This module keeps that calculation pure
so it can be unit tested without a display server.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Legacy geometry normalization keeps at least four available cells.
MINIMUM_COLUMNS = 4

Cell = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Monitor:
    """One display, in the compositor's logical pixel coordinates."""

    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def zone(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def contains_row(self, other: "Monitor") -> bool:
        """True when two monitors overlap vertically, so they share a row."""

        return self.y < other.y + other.height and other.y < self.y + self.height


def arrange(monitors: list[Monitor]) -> list[Cell]:
    """Return the ``(row, column)`` cell for each monitor, in input order.

    Monitors that overlap vertically share a row and are ordered left to
    right; otherwise a new row starts below, mirroring the desktop layout.
    """

    order = sorted(range(len(monitors)), key=lambda index: (monitors[index].y, monitors[index].x))
    cells: list[Cell | None] = [None] * len(monitors)
    rows: list[list[int]] = []
    for index in order:
        monitor = monitors[index]
        for row in rows:
            if any(monitors[member].contains_row(monitor) for member in row):
                row.append(index)
                break
        else:
            rows.append([index])
    for row_number, row in enumerate(rows):
        for column, index in enumerate(sorted(row, key=lambda item: monitors[item].x)):
            cells[index] = (row_number, column)
    return [cell for cell in cells if cell is not None]


def default_host_cell(monitors: list[Monitor], cells: list[Cell], position: str, zone: list[int]) -> Cell:
    """Place the host next to the monitor it is configured to hand over to."""

    attached = 0
    for index, monitor in enumerate(monitors):
        if zone and monitor.zone == list(zone):
            attached = index
            break
    else:
        attached = _extreme_monitor(monitors, position)
    row, column = cells[attached]
    if position == "left":
        return (row, column - 1)
    if position == "top":
        return (row - 1, column)
    if position == "bottom":
        return (row + 1, column)
    return (row, column + 1)


def _extreme_monitor(monitors: list[Monitor], position: str) -> int:
    keys = {
        "left": lambda monitor: monitor.x,
        "right": lambda monitor: -(monitor.x + monitor.width),
        "top": lambda monitor: monitor.y,
        "bottom": lambda monitor: -(monitor.y + monitor.height),
    }
    key = keys.get(position, keys["right"])
    return min(range(len(monitors)), key=lambda index: key(monitors[index]))


def resolve_placement(
    monitors: list[Monitor], cells: list[Cell], host_cell: Cell
) -> tuple[str, list[int]]:
    """Return the ``(edge, zone)`` implied by the host thumbnail's cell.

    The edge names the side of that monitor the pointer leaves through, which
    is exactly the pointer barrier the input capture portal is asked for.
    """

    row, column = host_cell
    neighbours = {
        "right": (row, column - 1),
        "left": (row, column + 1),
        "bottom": (row - 1, column),
        "top": (row + 1, column),
    }
    for edge, cell in neighbours.items():
        if cell in cells:
            return edge, monitors[cells.index(cell)].zone
    # Nothing is orthogonally adjacent, so fall back to the nearest monitor in
    # the direction the thumbnail was dropped.
    same_row = [index for index, cell in enumerate(cells) if cell[0] == row]
    if same_row:
        left = [index for index in same_row if cells[index][1] < column]
        if left:
            index = max(left, key=lambda item: cells[item][1])
            return "right", monitors[index].zone
        index = min(same_row, key=lambda item: cells[item][1])
        return "left", monitors[index].zone
    above = [index for index, cell in enumerate(cells) if cell[0] < row]
    if above:
        index = max(above, key=lambda item: cells[item][0])
        return "bottom", monitors[index].zone
    index = min(range(len(cells)), key=lambda item: cells[item][0])
    return "top", monitors[index].zone


def is_exterior(monitors: list[Monitor], edge: str, zone: list[int]) -> bool:
    """True when the edge faces away from every other monitor.

    A compositor only accepts pointer barriers on the outer boundary of the
    desktop, so an inner edge between two monitors can never hand over.
    """

    target = next((monitor for monitor in monitors if monitor.zone == list(zone)), None)
    if target is None:
        return False
    others = [monitor for monitor in monitors if monitor is not target]
    if edge == "right":
        return not any(
            other.x >= target.x + target.width and target.contains_row(other)
            for other in others
        )
    if edge == "left":
        return not any(
            other.x + other.width <= target.x and target.contains_row(other)
            for other in others
        )
    horizontal = lambda other: (  # noqa: E731 - short local predicate
        other.x < target.x + target.width and target.x < other.x + other.width
    )
    if edge == "bottom":
        return not any(
            other.y >= target.y + target.height and horizontal(other) for other in others
        )
    return not any(other.y + other.height <= target.y and horizontal(other) for other in others)


def normalize(cells: list[Cell], host_cell: Cell, *, two_row: bool) -> tuple[list[Cell], Cell, int, int]:
    """Shift cells to non-negative coordinates and pad out the empty positions.

    An empty column is kept on both sides so the host thumbnail can always be
    dropped to the left or the right of the monitors; ``two_row`` adds the same
    room above and below. One monitor with the host beside it therefore draws
    the four thumbnails the Windows form shows.

    Returns the monitor cells, the host cell, and the row/column count.
    """

    everything = [*cells, host_cell]
    row_padding = 1 if two_row else 0
    row_offset = -min(row for row, _ in everything) + row_padding
    column_offset = -min(column for _, column in everything) + 1
    shifted = [(row + row_offset, column + column_offset) for row, column in cells]
    host = (host_cell[0] + row_offset, host_cell[1] + column_offset)
    rows = max(row for row, _ in [*shifted, host]) + 1 + row_padding
    columns = max(column for _, column in [*shifted, host]) + 2
    if rows == 1:
        columns = max(columns, MINIMUM_COLUMNS)
    return shifted, host, rows, columns


def read_monitors(display) -> list[Monitor]:
    """Read the connected monitors from a ``Gdk.Display``."""

    monitors: list[Monitor] = []
    listing = display.get_monitors()
    for index in range(listing.get_n_items()):
        item = listing.get_item(index)
        geometry = item.get_geometry()
        name = item.get_connector() or item.get_model() or f"Display {index + 1}"
        monitors.append(
            Monitor(
                name=name,
                x=geometry.x,
                y=geometry.y,
                width=geometry.width,
                height=geometry.height,
                primary=index == 0,
            )
        )
    return monitors
