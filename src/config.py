SCREEN_WIDTH = 1420
SCREEN_HEIGHT = 960

BG_COLOR = (20, 22, 30)
HEX_COLOR = (55, 60, 80)  # outline color
FRIENDLY_TILE_COLOR = (35, 70, 90)
ENEMY_TILE_COLOR = (90, 55, 55)
BENCH_TILE_COLOR = (60, 60, 70)

BOARD_ROWS_PER_SIDE = 4
BOARD_ROWS = BOARD_ROWS_PER_SIDE * 2
BOARD_COLS = 7
HEX_RADIUS = 38
BENCH_ROWS = 1
BENCH_GAP = 35
AI_OWNER_ID = -1

MAX_PLAYERS = 8
MIN_READY_TO_START = 2  # allow local testing; raise to 2+ for real matches

TICKS_PER_SECOND = 20
ATTACK_DELAY_TICKS = TICKS_PER_SECOND  # one second between attacks
BULLET_HIT_RADIUS = 14
BULLET_TTL_TICKS = TICKS_PER_SECOND * 3
MELEE_RANGE_THRESHOLD = 90  # units with range <= this are treated as melee (no visible projectile)
ACCEL_ATTACK_FACTOR = 2  # attack speed multiplier during accelerated combat

PREP_SECONDS = 30
COMBAT_SECONDS = 30
ACCEL_SECONDS = 10

PLAYER_START_HEALTH = 20
LOSS_HEALTH_PENALTY = 2

UNIT_STATS = {
    "Vanguard": {"hp": 1500, "dmg": 10, "range": 60, "speed": 1.8, "color": (200, 90, 90), "cost": 1},
    "Ranger": {"hp": 80, "dmg": 18, "range": 260, "speed": 2.2, "color": (90, 210, 140), "cost": 2},
    "Mage": {"hp": 70, "dmg": 30, "range": 180, "speed": 1.6, "color": (120, 140, 230), "cost": 3},
}

# File names in assets/ used by the pygame 클라이언트. Missing files gracefully fall back to circles.
UNIT_ART = {
    "Vanguard": {"idle": "vanguard.png", "attack": "vanguard_attack.png", "icon": "vanguard_icon.png"},
    "Ranger": {"idle": "ranger.png", "attack": "ranger_attack.png", "icon": "ranger_icon.png"},
    "Mage": {"idle": "mage.png", "attack": "mage_attack.png", "icon": "mage_icon.png"},
}
