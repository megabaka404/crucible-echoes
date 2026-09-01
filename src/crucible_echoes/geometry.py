from __future__ import annotations

BASE_COORDS = [(row, col) for row in range(4) for col in range(5)]
GIANT_COORDS = [(row, col) for row in range(5) for col in range(8)]
MINIMAL_COORDS = [(row, col) for row in range(3) for col in range(4)]
EXPANSION_COORD = (-1, 2)
GIANT_EXPANSION_COORD = (-1, 4)


def board_coords(expanded: bool, fun_mode: str = "none") -> list[tuple[int, int]]:
    """Return the coordinate layout for the selected entertainment mode.

    The normal layout remains byte-for-byte compatible with the original
    helper.  Giant mode uses a 5x8 grid and minimal mode a 3x4 grid; the
    existing blueprint expansion is retained as one extra cell in either
    layout.
    """
    giant = fun_mode == "giant"
    minimal = fun_mode == "minimal"
    coords = GIANT_COORDS if giant else MINIMAL_COORDS if minimal else BASE_COORDS
    if expanded:
        return [GIANT_EXPANSION_COORD if giant else EXPANSION_COORD] + coords
    return list(coords)


def adjacent_indices(coords: list[tuple[int, int]], index: int) -> list[int]:
    row, col = coords[index]
    return [
        other
        for other, (other_row, other_col) in enumerate(coords)
        if other != index and abs(other_row - row) <= 1 and abs(other_col - col) <= 1
    ]


def orthogonal_indices(coords: list[tuple[int, int]], index: int) -> list[int]:
    """Return only up/down/left/right neighbours for fun-mode boards."""
    row, col = coords[index]
    return [
        other
        for other, (other_row, other_col) in enumerate(coords)
        if other != index
        and ((other_row == row and abs(other_col - col) == 1)
             or (other_col == col and abs(other_row - row) == 1))
    ]


def is_edge(coord: tuple[int, int], *, max_row: int = 3, max_col: int = 4) -> bool:
    row, col = coord
    return coord in {EXPANSION_COORD, GIANT_EXPANSION_COORD} or row in (0, max_row) or col in (0, max_col)


def is_corner(coord: tuple[int, int], *, max_row: int = 3, max_col: int = 4) -> bool:
    return coord in {(0, 0), (0, max_col), (max_row, 0), (max_row, max_col)}
