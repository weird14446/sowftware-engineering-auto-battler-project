import asyncio
import random
import time
from typing import Dict, Optional

from config import (
    ACCEL_SECONDS,
    AI_OWNER_ID,
    COMBAT_SECONDS,
    LOSS_HEALTH_PENALTY,
    MAX_PLAYERS,
    MIN_READY_TO_START,
    PLAYER_START_HEALTH,
    PREP_SECONDS,
    TICKS_PER_SECOND,
    UNIT_STATS,
)
from network import read_message, send_message
from simulation import BattleSimulation


class PlayerSession:
    def __init__(self, player_id: int, writer: asyncio.StreamWriter) -> None:
        self.player_id = player_id
        self.writer = writer
        self.ready = False
        self.name = f"Player{player_id + 1}"
        self.health = PLAYER_START_HEALTH
        self.alive = True
        self.gold = 10
        self.in_game = False


class AutoBattlerServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 50007) -> None:
        self.host = host
        self.port = port
        self.sim = BattleSimulation()
        self.sessions: Dict[int, PlayerSession] = {}
        self.server: Optional[asyncio.base_events.Server] = None
        self.combat_start_time: Optional[float] = None
        self.accel_started = False
        self.phase_start_time: Optional[float] = None  # unused when prep timer disabled
        self.last_timer_broadcast: float = time.monotonic()
        self.last_state_broadcast: float = time.monotonic()
        self.round_number: int = 1

    # --- Connection management ---
    def next_player_id(self) -> Optional[int]:
        for pid in range(MAX_PLAYERS):
            if pid not in self.sessions:
                return pid
        return None

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        player_id = self.next_player_id()
        if player_id is None:
            await send_message(writer, {"type": "error", "message": "Server full"})
            writer.close()
            await writer.wait_closed()
            return

        session = PlayerSession(player_id, writer)
        self.sessions[player_id] = session
        await send_message(writer, {"type": "welcome", "player_id": player_id})
        await self.send_full_state(writer)
        await self.broadcast_lobby()
        await self.broadcast_state()

        try:
            while True:
                msg = await read_message(reader)
                if msg is None:
                    break
                await self.handle_message(session, msg)
        finally:
            await self.disconnect(session)

    async def disconnect(self, session: PlayerSession) -> None:
        self.remove_units_for_player(session.player_id)
        self.sessions.pop(session.player_id, None)
        session.writer.close()
        try:
            await session.writer.wait_closed()
        except Exception:
            pass
        await self.broadcast_lobby()
        await self.broadcast_state()

    # --- Player actions ---
    async def handle_message(self, session: PlayerSession, msg: Dict) -> None:
        if msg.get("type") == "ready":
            session.ready = bool(msg.get("ready", False))
            await self.broadcast_lobby()
            await self.maybe_start_combat()
        elif msg.get("type") == "spawn":
            unit_type = msg.get("unit_type")
            if unit_type not in UNIT_STATS:
                await self.send_error(session, "Unknown unit type")
                return
            cost = UNIT_STATS[unit_type]["cost"]
            if session.gold < cost:
                await self.send_error(session, "Not enough gold")
                return
            session.gold -= cost
            self.sim.spawn_unit(session.player_id, unit_type)
            await self.broadcast_state()
        elif msg.get("type") == "place_unit":
            unit_id = msg.get("unit_id")
            tile = msg.get("tile")
            if not isinstance(tile, dict):
                await self.send_error(session, "Invalid tile data")
                return
            if not self.validate_owner(unit_id, session.player_id):
                await self.send_error(session, "You do not own that unit")
                return
            ok = self.sim.place_unit_on_tile(
                unit_id, int(tile.get("row", -1)), int(tile.get("col", -1)), allowed_side=self.side_for_player(session)
            )
            if not ok:
                await self.send_error(session, "Could not place unit")
            await self.broadcast_state()
        elif msg.get("type") == "bench_unit":
            unit_id = msg.get("unit_id")
            if not self.validate_owner(unit_id, session.player_id):
                await self.send_error(session, "You do not own that unit")
                return
            if self.sim.phase != "placement":
                await self.send_error(session, "Cannot bench units during combat")
                return
            self.sim.move_unit_to_bench(unit_id)
            await self.broadcast_state()
        elif msg.get("type") == "sell_unit":
            unit_id = msg.get("unit_id")
            if self.sim.phase != "placement":
                await self.send_error(session, "Cannot sell units during combat")
                return
            if not self.validate_owner(unit_id, session.player_id):
                await self.send_error(session, "You do not own that unit")
                return
            unit = self.sim.units.get(unit_id)
            if not unit:
                await self.send_error(session, "Unit not found")
                return
            cost = UNIT_STATS.get(unit.unit_type, {}).get("cost", 1)
            refund = max(1, int(cost * 0.5))
            self.sim.remove_unit(unit_id)
            session.gold += refund
            await self.broadcast_state()
        elif msg.get("type") == "enter_game":
            session.in_game = True
            if self.all_alive_in_game():
                now = time.monotonic()
                self.phase_start_time = now
                self.last_timer_broadcast = now
            await self.broadcast_lobby()
            await self.broadcast_state()
        elif msg.get("type") == "set_name":
            new_name = msg.get("name")
            if isinstance(new_name, str) and new_name.strip():
                session.name = new_name.strip()[:24]
                await self.broadcast_lobby()
                await self.broadcast_state()
        elif msg.get("type") == "force_start":
            await self.force_start()
        else:
            await self.send_error(session, "Unknown command")

    def validate_owner(self, unit_id: Optional[str], player_id: int) -> bool:
        if not unit_id:
            return False
        unit = self.sim.units.get(unit_id)
        return unit is not None and unit.owner_id == player_id

    async def send_error(self, session: PlayerSession, message: str) -> None:
        await send_message(session.writer, {"type": "error", "message": message})

    def remove_units_for_player(self, player_id: int) -> None:
        for unit_id, unit in list(self.sim.units.items()):
            if unit.owner_id == player_id:
                if unit.tile_id:
                    self.sim.tile_occupants.pop(f"{unit.tile_id}:{unit.owner_id}", None)
                self.sim.units.pop(unit_id, None)
        session = self.sessions.get(player_id)
        if session:
            session.in_game = False
            self.phase_start_time = None

    # --- Game flow ---
    async def maybe_start_combat(self, *, force_timer: bool = False) -> None:
        if self.sim.phase != "placement":
            return
        if not self.sessions:
            return
        ready_players = [s for s in self.sessions.values() if s.ready and s.alive]
        if len(ready_players) < max(2, MIN_READY_TO_START):
            return
        if not force_timer and not all(s.ready for s in ready_players):
            return
        await self.force_start()

    async def force_start(self) -> None:
        if self.sim.phase != "placement":
            return
        if not self.all_alive_in_game():
            return
        pairs = self.make_pairs()
        if not pairs:
            return
        self.sim.prepare_pairs(pairs)
        self.sim.start_combat()
        self.combat_start_time = time.monotonic()
        self.accel_started = False
        self.phase_start_time = self.combat_start_time
        self.last_timer_broadcast = self.phase_start_time
        await self.broadcast_state()

    # --- Broadcast helpers ---
    async def broadcast(self, payload: Dict) -> None:
        for session in list(self.sessions.values()):
            await send_message(session.writer, payload)

    async def broadcast_state(self) -> None:
        payload = {
            "type": "state",
            **self.sim.as_payload(),
            "players": self.players_payload(),
            "timers": self.timer_payload(),
            "round": self.round_number,
        }
        await self.broadcast(payload)

    async def broadcast_lobby(self) -> None:
        await self.broadcast(
            {
                "type": "lobby",
                "players": self.players_payload(),
                "phase": self.sim.phase,
                "timers": self.timer_payload(),
                "round": self.round_number,
            }
        )

    async def send_full_state(self, writer: asyncio.StreamWriter) -> None:
        await send_message(
            writer,
            {
                "type": "state",
                **self.sim.as_payload(),
                "players": self.players_payload(),
                "timers": self.timer_payload(),
                "round": self.round_number,
            },
        )

    def players_payload(self) -> list:
        return [
            {
                "id": s.player_id,
                "ready": s.ready,
                "name": s.name,
                "health": s.health,
                "alive": s.alive,
                "gold": s.gold,
                "in_game": s.in_game,
            }
            for s in self.sessions.values()
        ]

    def timer_payload(self) -> dict:
        now = time.monotonic()
        if self.sim.phase == "combat":
            elapsed = now - (self.combat_start_time or now)
            accel = self.accel_started
            remaining_combat = max(0, COMBAT_SECONDS - elapsed)
            remaining_accel = 0 if not accel else max(0, COMBAT_SECONDS + ACCEL_SECONDS - elapsed)
            return {
                "phase": "combat" if not accel else "accelerated",
                "remaining": remaining_combat if not accel else remaining_accel,
            }
        else:
            # Prep timer disabled; show static value
            return {"phase": "placement", "remaining": PREP_SECONDS}

    def all_alive_in_game(self) -> bool:
        alive_sessions = [s for s in self.sessions.values() if s.alive]
        if not alive_sessions:
            return False
        return all(s.in_game for s in alive_sessions)

    def alive_count(self) -> int:
        return len([s for s in self.sessions.values() if s.alive])

    def all_ready_alive(self) -> bool:
        alive_sessions = [s for s in self.sessions.values() if s.alive and s.in_game]
        if not alive_sessions:
            return False
        return all(s.ready for s in alive_sessions)

    def alive_in_game_count(self) -> int:
        return len([s for s in self.sessions.values() if s.alive and s.in_game])

    def side_for_player(self, session: PlayerSession) -> str:
        # All players place on friendly (bottom) during placement; server mirrors opponent on combat start.
        return "friendly"

    def make_pairs(self) -> list:
        ready_ids = [s.player_id for s in self.sessions.values() if s.ready and s.alive]
        random.shuffle(ready_ids)
        pairs = []
        for i in range(0, len(ready_ids) - 1, 2):
            pairs.append((ready_ids[i], ready_ids[i + 1]))
        return pairs

    # --- Main loop ---
    async def game_loop(self) -> None:
        while True:
            prev_phase = self.sim.phase
            if self.sim.phase == "combat":
                self.sim.tick_combat()
                await self.broadcast_state()
                now = time.monotonic()
                elapsed = now - (self.combat_start_time or now)
                if elapsed >= COMBAT_SECONDS + ACCEL_SECONDS:
                    await self.resolve_combat(force_timeout=True)
                    continue
                if self.sim.is_combat_resolved():
                    await self.resolve_combat(force_timeout=False)
                    continue
                if elapsed >= COMBAT_SECONDS and not self.accel_started:
                    self.accel_started = True
                    self.sim.accelerated = True
                    await self.broadcast_state()
                await asyncio.sleep(1 / TICKS_PER_SECOND)
            else:
                now = time.monotonic()
                if self.last_timer_broadcast is None or now - self.last_timer_broadcast >= 1.0:
                    self.last_timer_broadcast = now
                    await self.broadcast_lobby()
                if self.last_state_broadcast is None or now - self.last_state_broadcast >= 1.0:
                    self.last_state_broadcast = now
                    await self.broadcast_state()
                await asyncio.sleep(0.15)
            if prev_phase == "combat" and self.sim.phase == "placement":
                for session in self.sessions.values():
                    session.ready = False
                await self.broadcast_lobby()

    async def resolve_combat(self, force_timeout: bool) -> None:
        snapshots = self.sim.match_snapshots()
        results = []
        # Ensure participants include paired players even if they had no surviving units.
        pair_map = {idx: (b, t) for idx, (b, t) in enumerate(self.sim.pairs)}
        for match_id, snap in snapshots.items():
            alive = set(snap["alive"])
            hp_totals = snap["hp_totals"]
            participants = set(snap.get("participants", hp_totals.keys()))
            if match_id in pair_map:
                participants.update(pair_map[match_id])
                for pid in pair_map[match_id]:
                    hp_totals.setdefault(pid, 0)
            if len(alive) == 1:
                winner = next(iter(alive))
                losers = [pid for pid in participants if pid != winner]
                if losers:
                    results.append((winner, losers))
            elif len(alive) == 0:
                losers = list(participants)
                if losers:
                    results.append((None, losers))
            elif force_timeout:
                winner = max(hp_totals.items(), key=lambda kv: kv[1])[0]
                losers = [pid for pid in participants if pid != winner]
                if losers:
                    results.append((winner, losers))
        # Apply health changes
        for _, losers in results:
            for lid in losers:
                session = self.sessions.get(lid)
                if session and session.alive:
                    session.health -= LOSS_HEALTH_PENALTY
                    if session.health <= 0:
                        session.alive = False
                        self.remove_units_for_player(session.player_id)
        self.sim.end_combat()
        self.combat_start_time = None
        self.accel_started = False
        self.round_number += 1
        for session in self.sessions.values():
            session.ready = False
        await self.broadcast_state()
        await self.broadcast_lobby()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle_client, host=self.host, port=self.port)
        print(f"Server listening on {self.host}:{self.port} (max {MAX_PLAYERS} players)")
        async with self.server:
            await asyncio.gather(self.server.serve_forever(), self.game_loop())


if __name__ == "__main__":
    try:
        asyncio.run(AutoBattlerServer().start())
    except KeyboardInterrupt:
        pass
