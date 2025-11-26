import math
import uuid
from typing import Dict, List, Optional, Tuple

from board import HexTile, build_board, find_tile, tile_lookup
from config import (
    AI_OWNER_ID,
    ATTACK_DELAY_TICKS,
    BOARD_ROWS_PER_SIDE,
    BOARD_COLS,
    BULLET_HIT_RADIUS,
    BULLET_TTL_TICKS,
    ACCEL_ATTACK_FACTOR,
    MELEE_RANGE_THRESHOLD,
    TICKS_PER_SECOND,
    UNIT_STATS,
)


class UnitState:
    def __init__(self, owner_id: int, unit_type: str) -> None:
        stats = UNIT_STATS[unit_type]
        self.id = str(uuid.uuid4())
        self.owner_id = owner_id
        self.unit_type = unit_type
        self.max_hp = float(stats["hp"])
        self.hp = self.max_hp
        self.damage = float(stats["dmg"])
        self.attack_range = float(stats["range"])
        # Scale movement speed to the lower server tick rate (baseline 60fps).
        self.move_speed = float(stats["speed"]) * (60.0 / TICKS_PER_SECOND)
        self.status = "bench"  # bench | board | dead
        self.tile_id: Optional[str] = None
        self.home_tile_id: Optional[str] = None
        self.x = 0.0
        self.y = 0.0
        self.attack_cooldown = 0
        self.last_attack_tick = -1000
        self.match_id: Optional[int] = None

    def to_payload(self) -> Dict:
        return {
            "id": self.id,
            "owner": self.owner_id,
            "type": self.unit_type,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "x": self.x,
            "y": self.y,
            "status": self.status,
            "tile_id": self.tile_id,
            "home_tile_id": self.home_tile_id,
            "attack_at": self.last_attack_tick,
            "match_id": self.match_id,
        }

    def reset_for_placement(self, tiles: Dict[str, HexTile]) -> None:
        self.hp = self.max_hp
        if self.tile_id and self.tile_id in tiles:
            tile = tiles[self.tile_id]
            self.x, self.y = tile.center
        self.status = "board" if self.tile_id else "bench"
        self.attack_cooldown = 0
        self.last_attack_tick = -1000


class BulletState:
    def __init__(
        self,
        x: float,
        y: float,
        target_id: str,
        damage: float,
        spawn_tick: int,
        match_id: Optional[int],
        visible: bool,
    ) -> None:
        self.x = x
        self.y = y
        self.target_id = target_id
        # Speed scaled to match server tick cadence.
        self.speed = 10.0 * (60.0 / TICKS_PER_SECOND)
        self.damage = damage
        self.active = True
        self.spawn_tick = spawn_tick
        self.match_id = match_id
        self.visible = visible

    def to_payload(self) -> Dict:
        return {"x": self.x, "y": self.y, "target": self.target_id, "match_id": self.match_id, "visible": self.visible}


class BattleSimulation:
    def __init__(self) -> None:
        self.tiles = build_board()
        self.tile_map = tile_lookup(self.tiles)
        self.units: Dict[str, UnitState] = {}
        # key = f"{tile.id}:{owner_id}" to keep per-owner occupancy
        self.tile_occupants: Dict[str, str] = {}
        self.bullets: List[BulletState] = []
        self.phase = "placement"
        self.tick = 0
        self.pairs: List[Tuple[int, int]] = []
        self.accelerated = False

    # --- Unit and placement management ---
    def spawn_unit(self, owner_id: int, unit_type: str) -> UnitState:
        unit = UnitState(owner_id, unit_type)
        self.units[unit.id] = unit
        return unit

    def place_unit_on_tile(
        self, unit_id: str, row: int, col: int, allow_enemy: bool = False, allowed_side: Optional[str] = None
    ) -> bool:
        unit = self.units.get(unit_id)
        tile = find_tile(self.tiles, row, col)
        if not unit or not tile or tile.is_bench or self.phase != "placement":
            return False
        if allowed_side and tile.side != allowed_side and not (allow_enemy and tile.side == "enemy"):
            return False
        occ_key = f"{tile.id}:{unit.owner_id}"
        occupant = self.tile_occupants.get(occ_key)
        if occupant and occupant != unit_id:
            return False
        # Release previous tile
        if unit.tile_id:
            prev_key = f"{unit.tile_id}:{unit.owner_id}"
            self.tile_occupants.pop(prev_key, None)

        unit.tile_id = tile.id
        if tile.side == "friendly":
            unit.home_tile_id = tile.id
        unit.x, unit.y = tile.center
        unit.status = "board"
        self.tile_occupants[occ_key] = unit.id
        return True

    def move_unit_to_bench(self, unit_id: str) -> bool:
        unit = self.units.get(unit_id)
        if not unit:
            return False
        if unit.tile_id:
            self.tile_occupants.pop(f"{unit.tile_id}:{unit.owner_id}", None)
        unit.tile_id = None
        unit.status = "bench"
        unit.x, unit.y = 0.0, 0.0
        return True

    def remove_unit(self, unit_id: str) -> None:
        unit = self.units.get(unit_id)
        if not unit:
            return
        if unit.tile_id:
            self.tile_occupants.pop(f"{unit.tile_id}:{unit.owner_id}", None)
        self.units.pop(unit_id, None)

    def mirror_tile(self, tile: HexTile, to_side: str) -> Optional[HexTile]:
        if tile.is_bench:
            return tile
        if to_side == "enemy" and tile.side == "friendly":
            enemy_row = BOARD_ROWS_PER_SIDE - 1 - (tile.row - BOARD_ROWS_PER_SIDE)
            enemy_col = (BOARD_COLS - 1) - tile.col
            return find_tile(self.tiles, enemy_row, enemy_col)
        if to_side == "friendly" and tile.side == "enemy":
            friendly_row = BOARD_ROWS_PER_SIDE + (BOARD_ROWS_PER_SIDE - 1 - tile.row)
            friendly_col = (BOARD_COLS - 1) - tile.col
            return find_tile(self.tiles, friendly_row, friendly_col)
        return tile

    def move_owner_to_side(self, owner_id: int, side: str) -> None:
        for unit in [u for u in self.units.values() if u.owner_id == owner_id]:
            if not unit.home_tile_id:
                continue
            home_tile = self.tile_map.get(unit.home_tile_id)
            if not home_tile:
                continue
            source_tile = home_tile
            target_tile = self.mirror_tile(source_tile, side) if side != "friendly" else source_tile
            target_tile = target_tile or self.find_open_tile(side, owner_id)
            if not target_tile:
                continue
            occ_key = f"{target_tile.id}:{owner_id}"
            if occ_key in self.tile_occupants and self.tile_occupants[occ_key] != unit.id:
                alt_tile = self.find_open_tile(side, owner_id)
                if alt_tile:
                    target_tile = alt_tile
                else:
                    continue
            if unit.tile_id:
                self.tile_occupants.pop(f"{unit.tile_id}:{unit.owner_id}", None)
            unit.tile_id = target_tile.id
            unit.x, unit.y = target_tile.center
            unit.status = "board"
            self.tile_occupants[f"{target_tile.id}:{owner_id}"] = unit.id

    def restore_home_positions(self) -> None:
        self.tile_occupants.clear()
        for unit in self.units.values():
            unit.match_id = None
            if unit.home_tile_id and unit.home_tile_id in self.tile_map:
                tile = self.tile_map[unit.home_tile_id]
                unit.tile_id = tile.id
                unit.x, unit.y = tile.center
                unit.status = "board"
                self.tile_occupants[f"{tile.id}:{unit.owner_id}"] = unit.id
            else:
                if unit.tile_id:
                    self.tile_occupants.pop(f"{unit.tile_id}:{unit.owner_id}", None)
                unit.tile_id = None
                unit.status = "bench"

    def prepare_pairs(self, pairs: List[Tuple[int, int]]) -> None:
        self.pairs = pairs
        self.tile_occupants.clear()
        paired_ids = {pid for pair in pairs for pid in pair}
        for pair_id, (bottom, top) in enumerate(pairs):
            self.move_owner_to_side(bottom, "friendly")
            self.move_owner_to_side(top, "enemy")
            for unit in self.units.values():
                if unit.owner_id in (bottom, top):
                    unit.match_id = pair_id
        for unit in self.units.values():
            if unit.owner_id not in paired_ids and unit.tile_id:
                self.move_unit_to_bench(unit.id)
                unit.match_id = None

    def find_open_tile(self, side: str, owner_id: int) -> Optional[HexTile]:
        candidates = [t for t in self.tiles if not t.is_bench and t.side == side]
        candidates.sort(key=lambda t: t.row, reverse=(side == "friendly"))
        for tile in candidates:
            occ_key = f"{tile.id}:{owner_id}"
            if occ_key not in self.tile_occupants:
                return tile
        return None

    # --- Combat loop ---
    def start_combat(self) -> None:
        self.phase = "combat"
        self.tick = 0
        self.bullets.clear()
        self.accelerated = False
        for unit in self.units.values():
            unit.reset_for_placement(self.tile_map)

    def end_combat(self) -> None:
        self.phase = "placement"
        self.bullets.clear()
        self.restore_home_positions()
        self.pairs = []
        for unit in self.units.values():
            if unit.status != "bench":
                unit.status = "board"
                unit.attack_cooldown = 0
                unit.hp = unit.max_hp
                if unit.tile_id and unit.tile_id in self.tile_map:
                    tile = self.tile_map[unit.tile_id]
                    unit.x, unit.y = tile.center

    def find_target(self, seeker: UnitState) -> Optional[UnitState]:
        candidate: Optional[UnitState] = None
        min_dist = float("inf")
        for unit in self.units.values():
            if (
                unit.owner_id == seeker.owner_id
                or unit.status != "board"
                or unit.hp <= 0
                or unit.match_id != seeker.match_id
            ):
                continue
            dist = math.hypot(unit.x - seeker.x, unit.y - seeker.y)
            if dist < min_dist:
                min_dist = dist
                candidate = unit
        return candidate

    def is_combat_resolved(self) -> bool:
        snapshots = self.match_snapshots()
        if not snapshots:
            return False
        for snap in snapshots.values():
            if len(snap["alive"]) > 1:
                return False
        return True

    def match_snapshots(self) -> Dict[int, Dict]:
        snapshots: Dict[int, Dict] = {}
        for u in self.units.values():
            if u.match_id is None:
                continue
            snap = snapshots.setdefault(u.match_id, {"alive": set(), "hp_totals": {}, "participants": set()})
            snap["participants"].add(u.owner_id)
            # Only board units with HP contribute to win condition and timeout tiebreak.
            if u.status == "board" and u.hp > 0:
                snap["alive"].add(u.owner_id)
                snap["hp_totals"][u.owner_id] = snap["hp_totals"].get(u.owner_id, 0) + max(u.hp, 0)
            else:
                snap["hp_totals"].setdefault(u.owner_id, 0)
        return snapshots

    def tick_combat(self) -> None:
        if self.phase != "combat":
            return

        self.tick += 1
        # Update units
        for unit in list(self.units.values()):
            if unit.status != "board" or unit.hp <= 0:
                continue

            target = self.find_target(unit)
            if not target:
                continue

            dist = math.hypot(target.x - unit.x, target.y - unit.y)
            if dist <= unit.attack_range:
                if unit.attack_cooldown <= 0:
                    delay = ATTACK_DELAY_TICKS
                    if self.accelerated:
                        delay = max(1, int(delay / ACCEL_ATTACK_FACTOR))
                    unit.attack_cooldown = delay
                    unit.last_attack_tick = self.tick
                    visible = unit.attack_range > MELEE_RANGE_THRESHOLD
                    self.bullets.append(
                        BulletState(unit.x, unit.y, target.id, unit.damage, self.tick, unit.match_id, visible)
                    )
            else:
                if dist != 0:
                    unit.x += (target.x - unit.x) / dist * unit.move_speed
                    unit.y += (target.y - unit.y) / dist * unit.move_speed

            if unit.attack_cooldown > 0:
                decrement = ACCEL_ATTACK_FACTOR if self.accelerated else 1
                unit.attack_cooldown -= decrement

        # Update bullets
        for bullet in list(self.bullets):
            target = self.units.get(bullet.target_id)
            if not target or target.hp <= 0:
                bullet.active = False
                continue
            if target.match_id != bullet.match_id:
                bullet.active = False
                continue
            if self.tick - bullet.spawn_tick > BULLET_TTL_TICKS:
                bullet.active = False
                continue

            dx = target.x - bullet.x
            dy = target.y - bullet.y
            dist = math.hypot(dx, dy)
            if dist <= max(BULLET_HIT_RADIUS, bullet.speed):
                target.hp -= bullet.damage
                bullet.active = False
                if target.hp <= 0:
                    target.status = "dead"
            else:
                step = min(bullet.speed, dist)
                bullet.x += (dx / dist) * step
                bullet.y += (dy / dist) * step

        self.bullets = [b for b in self.bullets if b.active]

        match_alive: Dict[Optional[int], set] = {}
        for u in self.units.values():
            if u.status == "board" and u.hp > 0:
                match_alive.setdefault(u.match_id, set()).add(u.owner_id)
        if match_alive and all(len(owners) <= 1 for owners in match_alive.values()):
            self.end_combat()

    # --- Serialization helpers ---
    def as_payload(self) -> Dict:
        return {
            "phase": self.phase,
            "tick": self.tick,
            "units": [u.to_payload() for u in self.units.values()],
            "bullets": [b.to_payload() for b in self.bullets],
            "pairs": [{"match_id": idx, "bottom": b, "top": t} for idx, (b, t) in enumerate(self.pairs)],
        }
