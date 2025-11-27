import argparse
import json
import math
import select
import socket
from typing import Dict, List, Optional, Tuple
from pathlib import Path

import pygame

from board import HexTile, build_board
from config import (
    BG_COLOR,
    BENCH_TILE_COLOR,
    ENEMY_TILE_COLOR,
    FRIENDLY_TILE_COLOR,
    HEX_COLOR,
    HEX_RADIUS,
    PLAYER_START_HEALTH,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    UNIT_ART,
    UNIT_STATS,
)


PLAYER_COLORS = [
    (220, 120, 120),
    (90, 180, 240),
    (120, 210, 140),
    (210, 140, 240),
    (240, 210, 140),
    (140, 140, 240),
    (200, 180, 160),
    (160, 220, 200),
]

ATTACK_FLASH_TICKS = 10
SHOP_CARD_W = 180
SHOP_CARD_H = 140
SHOP_CARD_MARGIN = 16
SHOP_ICON_SIZE = (160, 110)
BENCH_ICON_SIZE = (60, 60)


class AssetCache:
    def __init__(self) -> None:
        self.base = Path(__file__).resolve().parent.parent / "assets"
        self.cache: Dict[str, Optional[pygame.Surface]] = {}
        self.raw_cache: Dict[str, Optional[pygame.Surface]] = {}

    def get_sprite(self, unit_type: str, mode: str) -> Optional[pygame.Surface]:
        return self.get_sprite_scaled(unit_type, mode, (70, 70) if mode == "idle" else (74, 74))

    def get_sprite_scaled(self, unit_type: str, mode: str, size: Tuple[int, int]) -> Optional[pygame.Surface]:
        key = f"{unit_type}:{mode}:{size[0]}x{size[1]}"
        if key in self.cache:
            return self.cache[key]
        filename = UNIT_ART.get(unit_type, {}).get(mode)
        sprite = self._load_scaled(filename, size)
        self.cache[key] = sprite
        return sprite

    def get_icon(self, unit_type: str) -> Optional[pygame.Surface]:
        return self.get_icon_scaled(unit_type, (64, 64))

    def get_icon_scaled(self, unit_type: str, size: Tuple[int, int]) -> Optional[pygame.Surface]:
        key = f"{unit_type}:icon:{size[0]}x{size[1]}"
        if key in self.cache:
            return self.cache[key]
        filename = UNIT_ART.get(unit_type, {}).get("icon")
        icon = self._load_scaled(filename, size)
        self.cache[key] = icon
        return icon

    def _load_raw(self, filename: Optional[str]) -> Optional[pygame.Surface]:
        if not filename:
            return None
        if filename in self.raw_cache:
            return self.raw_cache[filename]
        path = self.base / filename
        if not path.exists():
            self.raw_cache[filename] = None
            return None
        try:
            img = pygame.image.load(str(path)).convert_alpha()
        except pygame.error:
            img = None
        self.raw_cache[filename] = img
        return img

    def _load_scaled(self, filename: Optional[str], size: Tuple[int, int]) -> Optional[pygame.Surface]:
        raw = self._load_raw(filename)
        if raw is None:
            return None
        return pygame.transform.smoothscale(raw, size)

    def _load_image(self, filename: Optional[str], scale: Tuple[int, int]) -> Optional[pygame.Surface]:
        # Legacy helper kept for compatibility; now uses raw + smoothscale.
        return self._load_scaled(filename, scale)


class NetworkClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.buffer = b""
        self.connected = False

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        try:
            self.sock.connect((self.host, self.port))
        except BlockingIOError:
            self.connected = True
            return
        except ConnectionRefusedError:
            self.connected = False
            return
        self.connected = True

    def poll(self) -> List[Dict]:
        if not self.connected or not self.sock:
            return []
        messages: List[Dict] = []
        readable, _, _ = select.select([self.sock], [], [], 0)
        if readable:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                data = b""
            if not data:
                self.connected = False
                return []
            self.buffer += data
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    messages.append(json.loads(line.decode("utf-8")))
                except json.JSONDecodeError:
                    continue
        return messages

    def send(self, payload: Dict) -> None:
        if not self.connected or not self.sock:
            return
        try:
            data = (json.dumps(payload) + "\n").encode("utf-8")
            self.sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            self.connected = False


class AutoBattlerClient:
    def __init__(self, host: str, port: int) -> None:
        pygame.init()
        pygame.display.set_caption("Auto Battler Client")
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("arial", 20)
        self.small_font = pygame.font.SysFont("arial", 16)
        self.big_font = pygame.font.SysFont("arial", 28)

        self.network = NetworkClient(host, port)
        self.network.connect()

        self.tiles = build_board()
        self.dragging_unit: Optional[str] = None
        self.drag_from_bench = False
        self.drag_pos = (0, 0)

        self.player_id: Optional[int] = None
        self.players: Dict[int, Dict] = {}
        self.units: List[Dict] = []
        self.phase = "placement"
        self.bullets: List[Dict] = []
        self.last_error: Optional[str] = None
        self.server_tick = 0
        self.gold = 0
        self.assets = AssetCache()
        self.current_match_id: Optional[int] = None
        self.is_flipped = False
        self.pairs: List[Dict] = []
        self.scene = "lobby"  # lobby | game
        self.timer = {"phase": "placement", "remaining": 0}
        self.round_number = 1
        self.name_input = ""
        self.name_active = False

        self.ready_button = pygame.Rect(SCREEN_WIDTH - 220, 40, 160, 48)
        self.match_button = pygame.Rect(SCREEN_WIDTH // 2 - 140, SCREEN_HEIGHT // 2 + 40, 280, 60)
        self.name_input_rect = pygame.Rect(SCREEN_WIDTH // 2 - 200, SCREEN_HEIGHT // 2 - 10, 260, 42)
        self.name_save_button = pygame.Rect(self.name_input_rect.right + 20, self.name_input_rect.y, 120, 42)
        self.tile_map = {f"{t.row}-{t.col}": t for t in self.tiles}
        board_tiles = [t for t in self.tiles if not t.is_bench]
        self.board_min_y = min(t.center_y for t in board_tiles)
        self.board_max_y = max(t.center_y for t in board_tiles)
        self.board_min_x = min(t.center_x for t in board_tiles)
        self.board_max_x = max(t.center_x for t in board_tiles)
        self.board_mid_x = (self.board_min_x + self.board_max_x) / 2.0
        self.board_mid_y = (self.board_min_y + self.board_max_y) / 2.0
        self.shop_slots = self.build_shop()

    def build_shop(self) -> List[Dict]:
        slots: List[Dict] = []
        bench_tiles = [t for t in self.tiles if t.is_bench]
        if bench_tiles:
            bench_bottom = max(t.center_y for t in bench_tiles)
            y = int(bench_bottom + BENCH_ICON_SIZE[1] + 40)
        else:
            y = int(self.board_max_y + 160)
        y = min(y, SCREEN_HEIGHT - SHOP_CARD_H - 20)
        unit_types = ["Vanguard", "Ranger", "Mage"]
        total_width = len(unit_types) * SHOP_CARD_W + (len(unit_types) - 1) * SHOP_CARD_MARGIN
        start_x = int((SCREEN_WIDTH - total_width) / 2)
        for idx, utype in enumerate(unit_types):
            rect = pygame.Rect(
                start_x + idx * (SHOP_CARD_W + SHOP_CARD_MARGIN),
                y,
                SHOP_CARD_W,
                SHOP_CARD_H,
            )
            slots.append({"type": utype, "rect": rect})
        return slots

    # --- Networking ---
    def handle_message(self, msg: Dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "welcome":
            self.player_id = msg.get("player_id")
            if self.player_id is not None:
                self.players[self.player_id] = {
                    "id": self.player_id,
                    "ready": False,
                    "name": f"Player{self.player_id + 1}",
                    "health": PLAYER_START_HEALTH,
                    "alive": True,
                    "gold": self.gold,
                }
                self.name_input = f"Player{self.player_id + 1}"
        elif msg_type == "state":
            self.phase = msg.get("phase", self.phase)
            self.units = msg.get("units", [])
            self.bullets = msg.get("bullets", [])
            self.server_tick = msg.get("tick", self.server_tick)
            self.pairs = msg.get("pairs", [])
            self.store_players(msg.get("players", []))
            if "timers" in msg:
                self.timer = msg["timers"]
            if "round" in msg:
                self.round_number = msg["round"]
            self.update_orientation()
        elif msg_type == "lobby":
            self.store_players(msg.get("players", []))
            self.phase = msg.get("phase", self.phase)
            if "timers" in msg:
                self.timer = msg["timers"]
            if "round" in msg:
                self.round_number = msg["round"]
        elif msg_type == "error":
            self.last_error = msg.get("message")

    def store_players(self, players: List[Dict]) -> None:
        parsed = {}
        for player in players:
            pid = player.get("id")
            if pid is None:
                continue
            try:
                parsed[int(pid)] = player
            except ValueError:
                continue
        self.players = parsed
        if self.player_id is not None and self.player_id in self.players:
            self.gold = self.players[self.player_id].get("gold", 0)
        else:
            self.gold = 0

    def update_orientation(self) -> None:
        self.current_match_id = None
        self.is_flipped = False
        if self.player_id is None or self.phase != "combat":
            return
        for pair in self.pairs:
            bottom = pair.get("bottom")
            top = pair.get("top")
            if bottom == self.player_id:
                self.current_match_id = pair.get("match_id")
                self.is_flipped = False
                return
            if top == self.player_id:
                self.current_match_id = pair.get("match_id")
                self.is_flipped = True
                return

    # --- UI Helpers ---
    def bench_slots(self, bench_count: int) -> List[Tuple[int, int]]:
        bench_tiles = sorted([t for t in self.tiles if t.is_bench], key=lambda t: t.col)
        if bench_tiles:
            coords = [(int(t.center_x), int(t.center_y)) for t in bench_tiles]
            # repeat pattern if more units than tiles
            if bench_count > len(coords):
                coords = (coords * ((bench_count // len(coords)) + 1))[:bench_count]
            return coords[:bench_count]
        base_x = 120
        base_y = SCREEN_HEIGHT - 150
        spacing = 80
        return [(base_x + i * spacing, base_y) for i in range(bench_count)]

    def nearest_tile(self, pos: Tuple[int, int]) -> Tuple[Optional[HexTile], float]:
        px, py = pos
        best_tile = None
        best_dist = float("inf")
        for tile in self.tiles:
            dist = math.hypot(tile.center_x - px, tile.center_y - py)
            if dist < best_dist:
                best_dist = dist
                best_tile = tile
        return best_tile, best_dist

    def get_unit_color(self, unit: Dict) -> Tuple[int, int, int]:
        owner = int(unit.get("owner", 0))
        return PLAYER_COLORS[owner % len(PLAYER_COLORS)]

    def draw_tiles(self) -> None:
        for tile in self.tiles:
            points = []
            r = HEX_RADIUS
            for i in range(6):
                angle_rad = math.radians(60 * i - 30)
                points.append(
                    (
                        tile.center_x + r * math.cos(angle_rad),
                        tile.center_y + r * math.sin(angle_rad),
                    )
                )
            if tile.is_bench:
                fill = BENCH_TILE_COLOR
            else:
                fill = FRIENDLY_TILE_COLOR if tile.side == "friendly" else ENEMY_TILE_COLOR
            pygame.draw.polygon(self.screen, fill, points)
            pygame.draw.polygon(self.screen, (30, 30, 40), points, 2)

    def draw_units(self) -> None:
        # Draw board units from authoritative state, with local mirroring if needed.
        board_units = sorted(
            [u for u in self.units if u.get("status") == "board" and u.get("hp", 0) > 0],
            key=lambda u: u.get("y", 0),
        )
        if self.player_id is not None:
            if self.phase != "combat":
                board_units = [u for u in board_units if u.get("owner") == self.player_id]
            else:
                board_units = [
                    u
                    for u in board_units
                    if u.get("owner") == self.player_id
                    or (self.current_match_id is not None and u.get("match_id") == self.current_match_id)
                ]
        for unit in board_units:
            if unit.get("status") != "board" or unit.get("hp", 0) <= 0:
                continue
            x, y = self.render_pos(unit)
            is_attacking = self.phase == "combat" and self.server_tick - unit.get("attack_at", -999) < ATTACK_FLASH_TICKS
            sprite = self.assets.get_sprite(unit["type"], "attack" if is_attacking else "idle")
            is_enemy = self.player_id is not None and unit.get("owner") != self.player_id
            if sprite:
                rect = sprite.get_rect(center=(x, y))
                self.screen.blit(sprite, rect)
            else:
                radius = 20
                color = UNIT_STATS[unit["type"]]["color"]
                pygame.draw.circle(self.screen, color, (x, y), radius)
            hp_ratio = max(unit["hp"], 0) / unit["max_hp"]
            pygame.draw.rect(self.screen, (60, 0, 0), (x - 18, y - 30, 36, 6))
            pygame.draw.rect(self.screen, (40, 200, 40), (x - 18, y - 30, 36 * hp_ratio, 6))

        # Draw bench units only for the local player.
        if self.player_id is None:
            return
        bench_units = sorted(
            [u for u in self.units if u.get("owner") == self.player_id and u.get("status") == "bench"],
            key=lambda u: u["id"],
        )
        slots = self.bench_slots(max(len(bench_units), 1))
        for idx, unit in enumerate(bench_units):
            x, y = slots[idx]
            sprite = self.assets.get_sprite_scaled(unit["type"], "idle", BENCH_ICON_SIZE)
            if sprite:
                rect = sprite.get_rect(center=(x, y))
                self.screen.blit(sprite, rect)
            else:
                radius = 18
                color = UNIT_STATS[unit["type"]]["color"]
                pygame.draw.circle(self.screen, color, (x, y), radius)
                label = self.font.render(unit["type"][0], True, (255, 255, 255))
                self.screen.blit(label, (x - label.get_width() // 2, y - label.get_height() // 2))

    def draw_bullets(self) -> None:
        for bullet in self.bullets:
            if not bullet.get("visible", True):
                continue
            if self.phase == "combat" and self.current_match_id is not None:
                if bullet.get("match_id") != self.current_match_id:
                    continue
            x, y = self.render_bullet_pos(bullet)
            pygame.draw.circle(self.screen, (240, 240, 60), (x, y), 5)

    def draw_ui(self) -> None:
        ready = self.players.get(self.player_id, {}).get("ready", False) if self.player_id is not None else False
        if self.scene == "game":
            pygame.draw.rect(self.screen, (50, 150, 70) if not ready else (150, 60, 60), self.ready_button)
            label = "Ready" if not ready else "Unready"
            self.draw_text(label, self.ready_button.centerx, self.ready_button.centery, center=True)
            self.draw_shop()
            gold_text = self.big_font.render(f"Gold: {self.gold}", True, (245, 215, 120))
            self.screen.blit(gold_text, (self.ready_button.x, self.ready_button.bottom + 10))
            my_hp = self.players.get(self.player_id, {}).get("health", 0) if self.player_id is not None else 0
            hp_text = self.big_font.render(f"HP: {my_hp}", True, (200, 90, 90))
            self.screen.blit(hp_text, (self.ready_button.x, self.ready_button.bottom + 44))

        phase_text = self.big_font.render(f"Phase: {self.phase}", True, (220, 220, 220))
        self.screen.blit(phase_text, (40, 30))
        self.draw_timer()

        if self.last_error:
            err = self.font.render(self.last_error, True, (240, 120, 120))
            self.screen.blit(err, (40, 70))

        y = 110
        self.draw_text("Players", 40, y)
        y += 26
        for pid in sorted(self.players):
            p = self.players[pid]
            hp = p.get("health", 0)
            alive = p.get("alive", True)
            status = "Ready" if p.get("ready") else "Waiting"
            life = "Alive" if alive else "Out"
            text = f"#{pid + 1} {p.get('name', 'Player')} | HP:{hp} | {status} | {life}"
            color = (160, 240, 160) if p.get("ready") else (200, 200, 200)
            label = self.font.render(text, True, color)
            self.screen.blit(label, (40, y))
            y += 22

        bench_tiles = [t for t in self.tiles if t.is_bench]
        if bench_tiles:
            info_y = min(t.center_y for t in bench_tiles) - 40
        else:
            info_y = SCREEN_HEIGHT - SHOP_CARD_H - 70
        self.draw_text("Drag from bench to board, drag off to bench", 40, info_y)
        self.draw_text("Press Ready to start when all are prepared", 40, info_y + 20)

        if self.dragging_unit:
            x, y = self.drag_pos
            pygame.draw.circle(self.screen, (255, 255, 255), (x, y), 22, 2)

    def draw_timer(self) -> None:
        phase = self.timer.get("phase", "placement")
        remaining = max(0, int(self.timer.get("remaining", 0)))
        player_count = len(self.players)
        label = f"Round {self.round_number} | Players: {player_count} | {phase.capitalize()} - {remaining}s"
        timer_surf = self.big_font.render(label, True, (230, 230, 240))
        timer_bg = timer_surf.get_rect(center=(SCREEN_WIDTH // 2, 30))
        pygame.draw.rect(self.screen, (40, 44, 64), timer_bg.inflate(20, 10), border_radius=10)
        pygame.draw.rect(self.screen, (90, 120, 200), timer_bg.inflate(20, 10), width=2, border_radius=10)
        self.screen.blit(timer_surf, timer_bg)

    def draw_lobby(self) -> None:
        # Simple modern lobby overlay
        gradient_top = (30, 32, 50)
        gradient_bottom = (14, 16, 28)
        for y in range(SCREEN_HEIGHT):
            t = y / SCREEN_HEIGHT
            r = int(gradient_top[0] * (1 - t) + gradient_bottom[0] * t)
            g = int(gradient_top[1] * (1 - t) + gradient_bottom[1] * t)
            b = int(gradient_top[2] * (1 - t) + gradient_bottom[2] * t)
            pygame.draw.line(self.screen, (r, g, b), (0, y), (SCREEN_WIDTH, y))

        card = pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT // 2 - 140, 440, 260)
        pygame.draw.rect(self.screen, (40, 44, 64), card, border_radius=16)
        pygame.draw.rect(self.screen, (90, 120, 200), card, width=2, border_radius=16)

        title = pygame.font.SysFont("arial", 38).render("Auto Battler Lobby", True, (240, 240, 255))
        self.screen.blit(title, (card.centerx - title.get_width() // 2, card.y + 30))

        subtitle = self.small_font.render("Match with others, then jump into placement.", True, (200, 205, 220))
        self.screen.blit(subtitle, (card.centerx - subtitle.get_width() // 2, card.y + 80))

        # Player count
        player_text = self.font.render(f"Players connected: {len(self.players)}", True, (210, 215, 230))
        self.screen.blit(player_text, (card.centerx - player_text.get_width() // 2, card.y + 120))

        btn_color = (90, 160, 255)
        hover = self.match_button.collidepoint(pygame.mouse.get_pos())
        if hover:
            btn_color = (120, 185, 255)
        pygame.draw.rect(self.screen, btn_color, self.match_button, border_radius=12)
        pygame.draw.rect(self.screen, (20, 32, 60), self.match_button, width=2, border_radius=12)
        self.draw_text("Find Match", self.match_button.centerx, self.match_button.centery, center=True, size=24)

        helper = self.small_font.render("Click to enter placement and ready up.", True, (190, 195, 210))
        self.screen.blit(helper, (card.centerx - helper.get_width() // 2, self.match_button.bottom + 12))

        # Name input
        pygame.draw.rect(
            self.screen,
            (90, 120, 200) if self.name_active else (60, 70, 100),
            self.name_input_rect,
            border_radius=8,
        )
        pygame.draw.rect(self.screen, (20, 32, 60), self.name_input_rect, width=2, border_radius=8)
        name_text = self.font.render(self.name_input or "Enter name", True, (240, 240, 240))
        self.screen.blit(name_text, (self.name_input_rect.x + 8, self.name_input_rect.y + 10))

        pygame.draw.rect(self.screen, (120, 185, 255), self.name_save_button, border_radius=8)
        pygame.draw.rect(self.screen, (20, 32, 60), self.name_save_button, width=2, border_radius=8)
        self.draw_text("Save Name", self.name_save_button.centerx, self.name_save_button.centery, center=True, size=18)

    def render_pos(self, unit: Dict) -> Tuple[int, int]:
        x, y = float(unit.get("x", 0)), float(unit.get("y", 0))
        tile_id = unit.get("tile_id")
        if self.player_id is None:
            return int(x), int(y)
        # Only flip if we are the top player in the pair
        if self.is_flipped:
            rx = self.board_mid_x - (x - self.board_mid_x)
            ry = self.board_mid_y - (y - self.board_mid_y)
            return int(rx), int(ry)
        return int(x), int(y)

    def render_bullet_pos(self, bullet: Dict) -> Tuple[int, int]:
        x, y = float(bullet.get("x", 0)), float(bullet.get("y", 0))
        if self.player_id is None:
            return int(x), int(y)
        if self.is_flipped:
            rx = self.board_mid_x - (x - self.board_mid_x)
            ry = self.board_mid_y - (y - self.board_mid_y)
            return int(rx), int(ry)
        return int(x), int(y)

    def draw_shop(self) -> None:
        mouse_pos = pygame.mouse.get_pos()
        for slot in self.shop_slots:
            rect: pygame.Rect = slot["rect"]
            unit_name: str = slot["type"]
            cost = UNIT_STATS[unit_name]["cost"]
            hovered = rect.collidepoint(mouse_pos)

            base_color = (68, 52, 90) if hovered else (54, 44, 70)
            border_color = (160, 100, 200)
            pygame.draw.rect(self.screen, base_color, rect, border_radius=8)
            pygame.draw.rect(self.screen, border_color, rect, width=2, border_radius=8)

            icon_area = pygame.Rect(rect.x + 10, rect.y + 8, rect.width - 20, rect.height - 52)
            icon = self.assets.get_icon_scaled(unit_name, (icon_area.width, icon_area.height))
            if icon:
                icon_rect = icon.get_rect(center=icon_area.center)
                self.screen.blit(icon, icon_rect)
            else:
                color = UNIT_STATS[unit_name]["color"]
                pygame.draw.rect(self.screen, color, icon_area, border_radius=8)

            # Name and cost bar
            name_surf = self.font.render(unit_name, True, (240, 240, 240))
            name_rect = name_surf.get_rect()
            name_rect.midleft = (rect.x + 10, rect.bottom - 18)
            self.screen.blit(name_surf, name_rect)

            cost_text = self.font.render(str(cost), True, (240, 210, 120))
            coin_x = rect.right - 26
            coin_y = rect.bottom - 20
            pygame.draw.circle(self.screen, (200, 170, 70), (coin_x, coin_y), 8)
            self.screen.blit(cost_text, (coin_x + 12, coin_y - cost_text.get_height() // 2))

    def draw_text(self, text: str, x: int, y: int, center: bool = False, size: int = 20) -> None:
        font = self.font if size == 20 else pygame.font.SysFont("arial", size)
        surf = font.render(text, True, (230, 230, 230))
        if center:
            rect = surf.get_rect(center=(x, y))
            self.screen.blit(surf, rect)
        else:
            self.screen.blit(surf, (x, y))

    # --- Input handling ---
    def pick_unit_at(self, pos: Tuple[int, int]) -> Optional[str]:
        if self.player_id is None:
            return None
        px, py = pos
        # Board units
        for unit in self.units:
            if unit.get("owner") != self.player_id or unit.get("status") != "board":
                continue
            dist = math.hypot(unit.get("x", 0) - px, unit.get("y", 0) - py)
            if dist < 24:
                self.drag_from_bench = False
                return unit["id"]

        # Bench units
        bench_units = sorted(
            [u for u in self.units if u.get("owner") == self.player_id and u.get("status") == "bench"],
            key=lambda u: u["id"],
        )
        slots = self.bench_slots(max(len(bench_units), 1))
        for idx, unit in enumerate(bench_units):
            x, y = slots[idx]
            if math.hypot(x - px, y - py) < 20:
                self.drag_from_bench = True
                return unit["id"]
        return None

    def handle_mouse_down(self, pos: Tuple[int, int]) -> None:
        if self.scene == "lobby":
            return
        if self.phase != "placement":
            return
        for slot in self.shop_slots:
            if slot["rect"].collidepoint(pos):
                self.network.send({"type": "spawn", "unit_type": slot["type"]})
                return
        if self.ready_button.collidepoint(pos):
            current_ready = self.players.get(self.player_id, {}).get("ready", False) if self.player_id is not None else False
            self.network.send({"type": "ready", "ready": not current_ready})
            return
        unit_id = self.pick_unit_at(pos)
        if unit_id:
            self.dragging_unit = unit_id
            self.drag_pos = pos

    def handle_sell(self, pos: Tuple[int, int]) -> None:
        if self.scene != "game" or self.phase != "placement":
            return
        unit_id = self.pick_unit_at(pos)
        if unit_id:
            self.network.send({"type": "sell_unit", "unit_id": unit_id})

    def send_name(self) -> None:
        name = self.name_input.strip()
        if not name:
            return
        self.network.send({"type": "set_name", "name": name})

    def handle_mouse_up(self, pos: Tuple[int, int]) -> None:
        if not self.dragging_unit or self.phase != "placement":
            self.dragging_unit = None
            return
        tile, dist = self.nearest_tile(pos)
        if tile and dist < HEX_RADIUS * 0.9:
            if tile.is_bench:
                self.network.send({"type": "bench_unit", "unit_id": self.dragging_unit})
            else:
                self.network.send({"type": "place_unit", "unit_id": self.dragging_unit, "tile": {"row": tile.row, "col": tile.col}})
        else:
            self.network.send({"type": "bench_unit", "unit_id": self.dragging_unit})
        self.dragging_unit = None

    def run(self) -> None:
        running = True
        while running:
            for msg in self.network.poll():
                self.handle_message(msg)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif self.scene == "lobby":
                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        if self.match_button.collidepoint(event.pos):
                            self.scene = "game"
                            self.network.send({"type": "enter_game"})
                            # Reset ready state on entering game scene
                            self.network.send({"type": "ready", "ready": False})
                        elif self.name_input_rect.collidepoint(event.pos):
                            self.name_active = True
                        elif self.name_save_button.collidepoint(event.pos):
                            self.send_name()
                            self.name_active = False
                        else:
                            self.name_active = False
                        continue
                    if event.type == pygame.KEYDOWN and self.name_active:
                        if event.key == pygame.K_RETURN:
                            self.send_name()
                            self.name_active = False
                        elif event.key == pygame.K_BACKSPACE:
                            self.name_input = self.name_input[:-1]
                        else:
                            if len(self.name_input) < 24:
                                self.name_input += event.unicode
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        self.handle_mouse_down(event.pos)
                    elif event.button == 3:
                        self.handle_sell(event.pos)
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    self.handle_mouse_up(event.pos)
                elif event.type == pygame.MOUSEMOTION and self.dragging_unit:
                    self.drag_pos = event.pos

            self.screen.fill(BG_COLOR)
            if self.scene == "lobby":
                self.draw_lobby()
            else:
                self.draw_tiles()
                self.draw_units()
                self.draw_bullets()
                self.draw_ui()
            pygame.display.flip()
            self.clock.tick(60)

        pygame.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto battler pygame client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50007)
    args = parser.parse_args()

    client = AutoBattlerClient(args.host, args.port)
    client.run()


if __name__ == "__main__":
    main()
