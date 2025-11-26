import math
from typing import Dict, List, Optional, Tuple

from config import BENCH_GAP, BENCH_ROWS, BOARD_COLS, BOARD_ROWS, BOARD_ROWS_PER_SIDE, HEX_RADIUS


class HexTile:
    def __init__(self, row: int, col: int, x: float, y: float, is_bench: bool = False) -> None:
        self.row = row
        self.col = col
        self.center_x = x
        self.center_y = y
        self.is_bench = is_bench
        self.side = self._assign_side()

    @property
    def id(self) -> str:
        return f"{self.row}-{self.col}"

    @property
    def center(self) -> Tuple[float, float]:
        return self.center_x, self.center_y

    def _assign_side(self) -> str:
        if self.is_bench:
            return "bench"
        if self.row < BOARD_ROWS_PER_SIDE:
            return "enemy"
        if self.row < BOARD_ROWS_PER_SIDE * 2:
            return "friendly"
        return "neutral"


def build_board() -> List[HexTile]:
    """Generate a compact hex board shared by server and client."""
    tiles: List[HexTile] = []
    start_x = 320
    start_y = 140
    r = HEX_RADIUS
    for row in range(BOARD_ROWS):
        for col in range(BOARD_COLS):
            cx = start_x + col * (r * 1.732) + (0 if row % 2 == 0 else r * 0.866)
            cy = start_y + row * (r * 1.5)
            tiles.append(HexTile(row, col, cx, cy, False))
    # Bench rows below the battlefield
    bench_start_y = start_y + BOARD_ROWS * (r * 1.5) + BENCH_GAP
    for bench_row in range(BENCH_ROWS):
        global_row = BOARD_ROWS + bench_row
        for col in range(BOARD_COLS):
            cx = start_x + col * (r * 1.732) + (0 if global_row % 2 == 0 else r * 0.866)
            cy = bench_start_y + bench_row * (r * 1.5)
            tiles.append(HexTile(global_row, col, cx, cy, True))
    return tiles


def tile_lookup(tiles: List[HexTile]) -> Dict[str, HexTile]:
    return {t.id: t for t in tiles}


def find_tile(tiles: List[HexTile], row: int, col: int) -> Optional[HexTile]:
    return next((t for t in tiles if t.row == row and t.col == col), None)
