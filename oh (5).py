import asyncio
import copy
import gc
import io
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

import discord
from discord import app_commands

from bcsfe import core

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pip install cryptography
    Fernet = None
    InvalidToken = Exception

# ============================================================
# 설정
# ============================================================
SHOP_NAME = "Axiom Lab"
ADMIN_IDS: set[int] = {409393737966419978}
GAME_VERSION = 120200
INT32_MAX = 2_147_483_647
SESSION_TIMEOUT = 600
QUICK_CATFOOD = 45000   # 빠른충전 통조림 (고정)

COUNTRIES = {"kr": "한국", "jp": "일본", "tw": "대만", "en": "글로벌"}
RARITIES = {0: "일반", 1: "EX", 2: "레어", 3: "슈퍼레어", 4: "울트라레어", 5: "레전드레어"}

LABELS = {
    "catfood": "통조림", "xp": "XP", "np": "NP",
    "normal_tickets": "냥코 티켓", "rare_tickets": "레어 티켓",
    "gold_tickets": "골드 티켓", "platinum_tickets": "플래티넘 티켓",
    "legend_tickets": "레전드 티켓", "lucky_tickets": "럭키 티켓",
}
PINNED = list(LABELS)
SAFE_CAPS = {"catfood": 45000, "xp": 99_999_999, "np": 9_999}

CATEGORIES = {
    "currency": ("자원", "통조림 · XP · NP · 티켓"),
    "cats": ("유닛", "해금 · 진화 · 레벨"),
    "stages": ("진행도", "스토리 · 보물 · 이벤트"),
    "gamatoto": ("기지", "가마토토 · 마타타비 · 캣아이즈 · 캣아미"),
    "misc": ("설정", "프리셋 · 필드 목록 · 옵션"),
}

BASE_DIR = Path(__file__).resolve().parent
PRESET_FILE = BASE_DIR / "presets.json"
BALANCE_FILE = BASE_DIR / "balance.json"
TEMPLATE_FILE = BASE_DIR / "template.txt"
BACKUP_DIR = BASE_DIR / "backups"
SEC_FILE = BASE_DIR / "security.json"
AUDIT_FILE = BASE_DIR / "audit.log"

# ---------------- 랜덤 계정 밸런스 ----------------
MAX_BASE_LEVEL = 50
TIERS = {
    "basic": ("기본", {
        "progress": (0.00, 0.35), "catfood": (300, 3000), "cats": (4, 14),
        "chapters": (0, 2), "level": (1, 12), "rarity_w": (60, 15, 20, 4, 1, 0),
    }),
    "mid": ("중급", {
        "progress": (0.30, 0.70), "catfood": (3000, 20000), "cats": (14, 45),
        "chapters": (2, 6), "level": (8, 28), "rarity_w": (40, 12, 30, 14, 4, 0.3),
    }),
    "high": ("고급", {
        "progress": (0.65, 1.00), "catfood": (15000, 45000), "cats": (45, 110),
        "chapters": (6, 12), "level": (20, 42), "rarity_w": (30, 10, 30, 20, 9, 1),
    }),
}
TICKET_CAPS = {"normal": 40, "rare": 12, "gold": 6, "platinum": 2, "legend": 1, "lucky": 5}
TICKET_ORDER = ["normal", "rare", "gold", "platinum", "legend", "lucky"]
RARITY_LEVEL_MULT = {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.65, 4: 0.5, 5: 0.4}
DEFAULT_TIERS = copy.deepcopy(TIERS)

PARAMS = {
    "progress": ("진행도 (0~1)", "float", 0.0, 1.0),
    "catfood": ("통조림", "int", 0, SAFE_CAPS["catfood"]),
    "cats": ("보유 유닛 수", "int", 0, 300),
    "chapters": ("클리어 챕터 수", "int", 0, 30),
    "level": ("기본 레벨", "int", 1, MAX_BASE_LEVEL),
    "rarity_w": ("희귀도 가중치", "weights", 0, 1000),
}


# ============================================================
# 디자인
# ============================================================
class Theme:
    MAIN = discord.Color.from_rgb(79, 84, 112)
    ACCENT = discord.Color.from_rgb(236, 130, 170)
    BUSY = discord.Color.from_rgb(130, 134, 150)
    OK = discord.Color.from_rgb(86, 190, 150)
    ERR = discord.Color.from_rgb(224, 92, 92)


DIVIDER = "─" * 22


def fancy(text: str) -> str:
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D434 + ord(ch) - 65))
        elif ch == "h":
            out.append("ℎ")
        elif "a" <= ch <= "z":
            out.append(chr(0x1D44E + ord(ch) - 97))
        else:
            out.append(ch)
    return "".join(out)


def bar(value: int, cap: int, width: int = 10) -> str:
    ratio = 0.0 if cap <= 0 else min(1.0, value / cap)
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def load_token() -> str:
    token = os.getenv("DISCORD_TOKEN")
    if token:
        return token.strip()
    f = BASE_DIR / "token.txt"
    if f.exists():
        token = f.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise SystemExit("[AXIOM] token.txt에 봇 토큰을 넣거나 DISCORD_TOKEN 환경변수를 설정하세요.")


def label_of(name: str) -> str:
    return LABELS.get(name, name)


def cap_of(name: str) -> int:
    if name in SAFE_CAPS:
        return SAFE_CAPS[name]
    if "ticket" in name.lower():
        return 999
    return INT32_MAX


# ============================================================
# 프리셋 / 밸런스 / 보안 저장
# ============================================================
class PresetStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def list(self, uid: int) -> dict[str, dict[str, int]]:
        return self._read().get(str(uid), {})

    def save(self, uid: int, name: str, data: dict[str, int]) -> None:
        all_ = self._read()
        all_.setdefault(str(uid), {})[name] = data
        self.path.write_text(json.dumps(all_, ensure_ascii=False, indent=2), encoding="utf-8")


presets = PresetStore(PRESET_FILE)


def save_balance() -> None:
    data = {
        "tiers": {k: v[1] for k, v in TIERS.items()},
        "ticket_caps": TICKET_CAPS,
        "level_mult": {str(k): v for k, v in RARITY_LEVEL_MULT.items()},
    }
    BALANCE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_balance() -> None:
    try:
        data = json.loads(BALANCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for k, params in data.get("tiers", {}).items():
        if k in TIERS:
            for pk, pv in params.items():
                if pk in TIERS[k][1]:
                    TIERS[k][1][pk] = tuple(pv)
    TICKET_CAPS.update({k: int(v) for k, v in data.get("ticket_caps", {}).items()})
    RARITY_LEVEL_MULT.update({int(k): float(v) for k, v in data.get("level_mult", {}).items()})


load_balance()

SEC = {
    "allowlist_on": False,
    "allowed": [],
    "blocked": [],
    "cooldown": 0,
    "create_admin_only": False,
    "log_on": True,
}
_last_use: dict[int, float] = {}


def save_sec() -> None:
    SEC_FILE.write_text(json.dumps(SEC, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sec() -> None:
    try:
        data = json.loads(SEC_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for k in SEC:
        if k in data:
            SEC[k] = data[k]


load_sec()

KIND_KO = {"menu": "에디터", "quick": "무료통조림", "create": "랜덤계정", "save": "세이브관리"}


def kind_ko(kind: str) -> str:
    base, _, tail = kind.partition(":")
    name = KIND_KO.get(base, base)
    return f"{name} 사용 거부됨" if tail == "거부" else f"{name} 사용"


def audit(uid: int, event: str) -> None:
    """코드/비밀번호는 절대 기록하지 않음. 기록은 한국어로 저장."""
    if not SEC["log_on"]:
        return
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{uid}\t{kind_ko(event)}\n")
    except OSError:
        pass


async def guard(interaction: discord.Interaction, kind: str) -> bool:
    uid = interaction.user.id
    if uid in ADMIN_IDS:
        audit(uid, kind)
        return True
    reason = None
    if uid in SEC["blocked"]:
        reason = "차단된 유저입니다."
    elif SEC["allowlist_on"] and uid not in SEC["allowed"]:
        reason = "허용된 유저만 사용할 수 있습니다."
    elif kind == "create" and SEC["create_admin_only"]:
        reason = "이 명령어는 관리자 전용입니다."
    else:
        wait = SEC["cooldown"] - (time.monotonic() - _last_use.get(uid, -1e9))
        if wait > 0:
            reason = f"{int(wait) + 1}초 뒤에 다시 시도하세요."
    if reason:
        audit(uid, f"{kind}:거부")
        await interaction.response.send_message(reason, ephemeral=True)
        return False
    _last_use[uid] = time.monotonic()
    audit(uid, kind)
    return True


# ============================================================
# BCSFE 래퍼 (동기 → executor)
# ============================================================
_core_ready = False


def init_core() -> None:
    global _core_ready
    if _core_ready:
        return
    cd = getattr(core, "core_data", None)
    if cd is not None and hasattr(cd, "init_data"):
        cd.init_data()
    _core_ready = True


def make_country_code(code: str):
    cls = core.CountryCode
    for name in ("from_code", "from_str", "from_string"):
        fn = getattr(cls, name, None)
        if callable(fn):
            try:
                return fn(code)
            except Exception:
                pass
    try:
        return cls(code)
    except Exception:
        pass
    raise RuntimeError(f"CountryCode 생성 실패. 멤버: {[m for m in dir(cls) if not m.startswith('_')]}")


def scan_int_fields(save_file) -> dict[str, int]:
    out = {}
    for name, val in vars(save_file).items():
        if name.startswith("_") or isinstance(val, bool) or not isinstance(val, int):
            continue
        out[name] = val
    getter = getattr(save_file, "get_catfood", None)
    if callable(getter):
        try:
            out["catfood"] = getter()
        except Exception:
            pass
    return out


def write_field(save_file, name: str, value: int) -> None:
    setter = getattr(save_file, f"set_{name}", None)
    if callable(setter):
        setter(value)
    else:
        setattr(save_file, name, value)


def download_save(transfer: str, confirm: str, country: str):
    try:
        init_core()
        cc = make_country_code(country)
        handler, _ = core.ServerHandler.from_codes(transfer, confirm, cc, core.GameVersion(GAME_VERSION))
        if handler is None:
            msg = "이어하기 코드/비밀번호가 틀렸거나 국가 선택이 잘못됨"
            if country == "jp":
                msg += " (일본/대만 서버 헷갈리기 쉬움)"
            return None, msg
        return handler.save_file, ""
    except Exception as e:
        print(f"[AXIOM] 다운로드 오류: {type(e).__name__}", file=sys.stderr)
        return None, f"{type(e).__name__}: {e}"


def backup_save(sf, uid: int) -> str | None:
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"{uid}_{datetime.now():%Y%m%d_%H%M%S}.sav"
        sf.to_data().to_file(core.Path(str(path)))
        return path.name
    except Exception as e:
        print(f"[AXIOM] 백업 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def make_limit_getter(sf):
    """UnitLimit의 첫 값을 최대 기본 레벨로 가정함(미검증). 못 읽으면 MAX_BASE_LEVEL."""
    try:
        ul = sf.cats.read_unitlimit(sf)
    except Exception:
        ul = None

    def get(cat_id: int) -> int:
        try:
            d = ul.get_unit_limit(cat_id) if ul else None
            if d and d.values:
                v = int(d.values[0])
                if 1 <= v <= 200:
                    return v
        except Exception:
            pass
        return MAX_BASE_LEVEL

    return get


# ---------------- 유닛 작업 ----------------
def find_cats(sf, query: str) -> list:
    q = query.strip()
    if q.isdigit():
        cat = sf.cats.get_cat_by_id(int(q))
        return [cat] if cat else []
    return sf.cats.get_cats_name(sf, q)


def op_unlock_all(sf) -> int:
    cats = sf.cats.get_cats_obtainable(sf)
    if cats is None:
        cats = sf.cats.get_all_cats()
    n = 0
    for c in cats:
        if not c.unlocked:
            c.unlock(sf)
            n += 1
    return n


def op_unlock_rarity(sf, rarity: int) -> int:
    n = 0
    for c in sf.cats.get_cats_rarity(sf, rarity):
        if not c.unlocked:
            c.unlock(sf)
            n += 1
    return n


def op_unlock_query(sf, query: str) -> int:
    cats = find_cats(sf, query)
    for c in cats:
        c.unlock(sf)
    return len(cats)


def op_remove_query(sf, query: str) -> int:
    cats = find_cats(sf, query)
    for c in cats:
        c.remove(reset=True, save_file=sf)
    return len(cats)


def op_true_forms(sf) -> int:
    cats = [c for c in sf.cats.get_all_cats() if c.unlocked]
    sf.cats.true_form_cats(sf, cats, force=False, set_current_forms=True)
    return len(cats)


def op_fourth_forms(sf) -> int:
    cats = [c for c in sf.cats.get_all_cats() if c.unlocked]
    sf.cats.fourth_form_cats(sf, cats, force=False, set_current_forms=True)
    return len(cats)


def _apply_levels(sf, cats: list, base: int, plus: int) -> str:
    get_max = make_limit_getter(sf)
    clamped = 0
    for c in cats:
        lv = min(base, get_max(c.id))
        if lv < base:
            clamped += 1
        c.set_upgrade(sf, core.Upgrade(plus=plus, base=lv))
    return f"{len(cats)}마리" + (f" (상한 적용 {clamped}마리)" if clamped else "")


def op_set_level(sf, query: str, base: int, plus: int) -> str:
    return _apply_levels(sf, find_cats(sf, query), base, plus)


def op_level_all_unlocked(sf, base: int, plus: int) -> str:
    return _apply_levels(sf, [c for c in sf.cats.get_all_cats() if c.unlocked], base, plus)


# ---------------- 진행도 작업 ----------------
def parse_ranges(text: str, limit: int) -> list[int]:
    out: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = sorted((int(a), int(b)))
                out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(n for n in out if 1 <= n <= limit)


def story_chapter_count(sf) -> int:
    try:
        return len(sf.story.get_real_chapters())
    except Exception:
        return 0


def op_clear_story(sf) -> int:
    n = 0
    for ch in sf.story.get_real_chapters():
        ch.clear_chapter()
        n += 1
    return n


def op_clear_story_chapters(sf, chapters: list[int]) -> int:
    real = sf.story.get_real_chapters()
    n = 0
    for i in chapters:
        if 1 <= i <= len(real):
            real[i - 1].clear_chapter()
            n += 1
    return n


def op_clear_story_stages(sf, chapter: int, stages: list[int]) -> int:
    real = sf.story.get_real_chapters()
    if not (1 <= chapter <= len(real)):
        return 0
    ch = real[chapter - 1]
    n = 0
    for s in stages:
        try:
            ch.clear_stage(s - 1, 1)
            n += 1
        except Exception:
            pass
    return n


def op_clear_treasures(sf, level: int) -> int:
    n = 0
    for ch in sf.story.get_real_chapters():
        for st in ch.get_valid_treasure_stages():
            st.set_treasure(level)
            n += 1
    return n


def _clear_nested(obj, amount: int = 1) -> int:
    n = 0
    for m, cs in enumerate(obj.chapters):
        for s, _ in enumerate(cs.chapters):
            for st in range(obj.get_total_stages(m, s)):
                try:
                    obj.clear_stage(m, s, st, amount, True)
                    n += 1
                except Exception:
                    pass
    return n


def op_clear_legend(sf) -> int:
    return _clear_nested(sf.legend_quest)


def op_clear_zero_legends(sf) -> int:
    return _clear_nested(sf.zero_legends)


def op_clear_events(sf) -> int:
    ev = sf.event_stages
    n = 0
    for t in range(len(ev.chapters)):
        try:
            ev.clear_group(t, True)
            n += 1
        except Exception:
            pass
    return n


# ---------------- 기지 / 아이템 작업 ----------------
def _resolve(sf, path: str):
    obj = sf
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def op_gamatoto_xp(sf, value: int) -> str:
    sf.gamatoto.xp = value
    return f"{value:,} 설정"


def op_set_all_items(sf, path: str, value: int) -> int:
    seq = _resolve(sf, path)
    n = 0
    for i, v in enumerate(seq):
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            seq[i] = value
            n += 1
        elif hasattr(v, "amount"):
            v.amount = value
            n += 1
    return n


def fmt_result(r) -> str:
    return f"{r}개" if isinstance(r, int) else str(r)


# ---------------- 적용 + 업로드 ----------------
def apply_and_upload(save_file, changes: dict[str, int], new_account: bool, ops):
    results = []
    try:
        for desc, fn in ops:
            try:
                results.append(f"{desc}: {fmt_result(fn(save_file))}")
            except Exception as e:
                results.append(f"{desc}: 실패 ({type(e).__name__}: {e})")
        for name, value in changes.items():
            write_field(save_file, name, value)
        if new_account:
            try:
                core.ServerHandler(save_file).create_new_account()
            except Exception:
                pass
        codes = core.ServerHandler(save_file).get_codes()
        if codes is None:
            return None, "서버 업로드 실패 (인터넷 확인 후 다시 시도)", results
        return codes, "", results
    except Exception as e:
        print(f"[AXIOM] 업로드 오류: {type(e).__name__}", file=sys.stderr)
        return None, f"{type(e).__name__}: {e}", results


# ---------------- 랜덤 계정 (템플릿 복제 방식) ----------------
def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _jitter(t: float, spread: float = 0.10) -> float:
    return min(1.0, max(0.0, t + random.uniform(-spread, spread)))


def _pick_cats(sf, count: int, weights: tuple) -> list[tuple[object, int]]:
    obtainable = sf.cats.get_cats_obtainable(sf) or sf.cats.get_all_cats()
    ok_ids = {c.id for c in obtainable}
    pools = {r: [c for c in sf.cats.get_cats_rarity(sf, r) if c.id in ok_ids] for r in RARITIES}
    picked = []
    for _ in range(count):
        avail = [r for r in RARITIES if pools[r] and weights[r] > 0]
        if not avail:
            break
        r = random.choices(avail, weights=[weights[x] for x in avail])[0]
        picked.append((pools[r].pop(random.randrange(len(pools[r]))), r))
    return picked


def load_template():
    if not TEMPLATE_FILE.exists():
        return None
    lines = [l.strip() for l in TEMPLATE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    return lines[0], lines[1], (lines[2] if len(lines) > 2 else "kr")


def get_base_save():
    tpl = load_template()
    if tpl is None:
        return None, "kr", "template.txt 필요 (게임에서 새로 만든 부계정 코드 / 비번 / 국가, 3줄)"
    code, pw, country = tpl
    sf, err = download_save(code, pw, country)
    if sf is None:
        return None, country, f"템플릿 불러오기 실패: {err}"
    return sf, country, ""


_BASE_CACHE: dict = {"sf": None, "country": "kr"}
_BASE_LOCK = threading.Lock()


def cached_base():
    """템플릿을 서버에서 딱 한 번만 받아서 재사용. 동시에 여러 번 불러도 다운로드는 1번."""
    with _BASE_LOCK:
        if _BASE_CACHE["sf"] is None:
            sf, country, err = get_base_save()
            if sf is None:
                return None, country, err
            _BASE_CACHE.update(sf=sf, country=country)
        return _BASE_CACHE["sf"], _BASE_CACHE["country"], ""


BASE_COPY = False   # True: 템플릿을 한 번만 받아 복사(빠르지만 메모리 2배) / False: 매번 새로 받음(느리지만 메모리 절약)


def fresh_base():
    """BASE_COPY가 켜져 있으면 캐시된 템플릿의 복사본, 꺼져 있으면 새로 다운로드."""
    if not BASE_COPY:
        return get_base_save()
    sf, country, err = cached_base()
    if sf is None:
        return None, country, err
    try:
        return copy.deepcopy(sf), country, ""
    except Exception:
        return get_base_save()


def generate_one(tier_key: str):
    try:
        return _generate_one(tier_key)
    finally:
        gc.collect()   # 계정 하나 끝날 때마다 메모리 정리


def _generate_one(tier_key: str):
    sf, country, err = fresh_base()
    if sf is None:
        return None, err

    _, p = TIERS[tier_key]
    try:
        if not core.ServerHandler(sf).create_new_account():
            return None, "새 계정 생성 실패 (서버 거부 또는 인터넷 문제)"

        prog = random.triangular(*p["progress"])

        food = min(int(_lerp(*p["catfood"], _jitter(prog))), SAFE_CAPS["catfood"])
        write_field(sf, "catfood", food)

        for name in scan_int_fields(sf):
            low = name.lower()
            if "ticket" not in low:
                continue
            cap = next((c for k, c in TICKET_CAPS.items() if k in low), 5)
            write_field(sf, name, int(cap * _jitter(prog) * random.uniform(0.3, 1.0)))

        want = int(_lerp(*p["cats"], _jitter(prog)))
        picked = _pick_cats(sf, want, p["rarity_w"])
        get_max = make_limit_getter(sf)
        base_lv = _lerp(*p["level"], _jitter(prog))
        for cat, r in picked:
            cat.unlock(sf)
            lv = int(base_lv * RARITY_LEVEL_MULT[r] * random.uniform(0.7, 1.2))
            lv = max(1, min(lv, get_max(cat.id)))
            try:
                cat.set_upgrade(sf, core.Upgrade(plus=0, base=lv))
            except Exception:
                pass

        n_ch = min(int(_lerp(*p["chapters"], _jitter(prog))), story_chapter_count(sf))
        if n_ch:
            op_clear_story_chapters(sf, list(range(1, n_ch + 1)))

        codes = core.ServerHandler(sf).get_codes()
        if codes is None:
            return None, "서버 업로드 실패"
        return {"codes": codes}, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def simulate(tier_key: str, n: int = 200) -> dict:
    p = TIERS[tier_key][1]
    rows = {"통조림": [], "유닛": [], "챕터": [], "레벨": []}
    for _ in range(n):
        prog = random.triangular(*p["progress"])
        rows["통조림"].append(min(int(_lerp(*p["catfood"], _jitter(prog))), SAFE_CAPS["catfood"]))
        rows["유닛"].append(int(_lerp(*p["cats"], _jitter(prog))))
        rows["챕터"].append(int(_lerp(*p["chapters"], _jitter(prog))))
        rows["레벨"].append(int(_lerp(*p["level"], _jitter(prog))))
    return {k: (min(v), sum(v) // n, max(v)) for k, v in rows.items()}


# ============================================================
# 세션 / 임베드
# ============================================================
@dataclass
class Session:
    save_file: object
    original: dict[str, int]
    country: str
    category: str = "home"
    pending: dict[str, int] = field(default_factory=dict)
    ops: list = field(default_factory=list)
    new_account: bool = True
    busy: bool = False
    backup: str | None = None
    touched: float = field(default_factory=time.time)
    active: object = None


def _safe(fn, default="?"):
    try:
        return fn()
    except Exception:
        return default


def build_embed(s: Session, note: str = "") -> discord.Embed:
    e = discord.Embed(title=SHOP_NAME, color=Theme.MAIN)

    if s.category == "home":
        e.description = note or "아래에서 카테고리를 골라주세요."
        e.add_field(name="원본 백업", value=f"`{s.backup}`" if s.backup else "백업 실패", inline=False)
        if s.pending or s.ops:
            e.add_field(name="대기 중", value=f"수치 {len(s.pending)}건 · 작업 {len(s.ops)}건", inline=False)
        return e

    name, desc = CATEGORIES[s.category]
    e.description = f"**{name}** · {desc}" + (f"\n{note}" if note else "")
    sf = s.save_file

    if s.category == "currency":
        for key in PINNED:
            if key not in s.original:
                continue
            cur = s.original[key]
            new = s.pending.get(key)
            shown = new if new is not None else cur
            value = f"`{cur:,}` → **`{new:,}`**" if new is not None else f"`{cur:,}`"
            e.add_field(name=label_of(key) + (" *" if new is not None else ""), value=value, inline=True)
        shown_count = sum(1 for n in PINNED if n in s.original)
        for _ in range((3 - shown_count % 3) % 3):
            e.add_field(name="​", value="​", inline=True)
        others = [n for n in s.pending if n not in PINNED]
        if others:
            e.add_field(
                name="기타 변경",
                value="\n".join(f"`{n}` `{s.original[n]:,}` → `{s.pending[n]:,}`" for n in others[:10])[:1000],
                inline=False,
            )

    elif s.category == "cats":
        have = _safe(lambda: len(sf.cats.get_unlocked_cats()))
        total = _safe(lambda: len(sf.cats.get_all_cats()))
        e.add_field(name="보유 유닛", value=f"`{have}` / `{total}`", inline=True)

    elif s.category == "stages":
        e.add_field(name="스토리 챕터", value=f"`{story_chapter_count(sf)}`개", inline=True)

    elif s.category == "gamatoto":
        xp = _safe(lambda: f"{sf.gamatoto.xp:,}")
        e.add_field(name="가마토토 XP", value=f"`{xp}`", inline=True)

    elif s.category == "misc":
        e.add_field(name="새 계정 업로드", value="켜짐" if s.new_account else "꺼짐", inline=True)
        e.add_field(name="원본 백업", value=f"`{s.backup}`" if s.backup else "없음", inline=True)

    if s.ops:
        lines = [f"`{i + 1:02d}` {d}" for i, (d, _) in enumerate(s.ops[:12])]
        if len(s.ops) > 12:
            lines.append(f"… 외 {len(s.ops) - 12}건")
        e.add_field(name="작업 목록", value="\n".join(lines)[:1000], inline=False)

    e.set_footer(text=f"수치 {len(s.pending)}건 · 작업 {len(s.ops)}건 · {SESSION_TIMEOUT // 60}분 동안 조작이 없으면 종료돼요")
    return e


def busy_embed(text: str = "저장하고 업로드하는 중입니다.", done: int = 0, total: int = 0) -> discord.Embed:
    tail = f" ({done}/{total})" if total else ""
    return discord.Embed(description=f"{text}{tail}", color=Theme.BUSY)


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(title="실패", description=text, color=Theme.ERR)


def code_embed(codes, title: str = "이어하기 정보") -> discord.Embed:
    e = discord.Embed(title=title, color=Theme.OK)
    e.add_field(name="이어하기 코드", value=f"```{codes[0]}```", inline=True)
    e.add_field(name="비밀번호", value=f"```{codes[1]}```", inline=True)
    return e


async def send_dm(user: discord.abc.User, *, embeds=None, files=None) -> bool:
    try:
        dm = await user.create_dm()
        chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)] if embeds else [[]]
        for idx, chunk in enumerate(chunks):
            kwargs = {}
            if chunk:
                kwargs["embeds"] = chunk
            if idx == 0 and files:
                kwargs["files"] = files
            if kwargs:
                await dm.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def queue_op(interaction: discord.Interaction, panel: "PanelView", desc: str, fn) -> None:
    panel.session.ops.append((desc, fn))
    await interaction.response.edit_message(embed=build_embed(panel.session, f"작업 추가: {desc}"), view=panel)


async def refresh_panel(panel: "PanelView", note: str = "") -> None:
    if panel.message:
        try:
            await panel.message.edit(embed=build_embed(panel.session, note), view=panel)
        except discord.HTTPException:
            pass


# ============================================================
# 모달
# ============================================================
class ValueModal(discord.ui.Modal):
    def __init__(self, panel: "PanelView", name: str | None):
        super().__init__(title="값 변경" if name else "필드 직접 입력")
        self.panel, self.name = panel, name
        self.field_input = None
        if name is None:
            self.field_input = discord.ui.TextInput(label="필드명", placeholder="필드 목록에서 확인", max_length=64)
            self.add_item(self.field_input)
        s = panel.session
        self.value_input = discord.ui.TextInput(
            label=f"{label_of(name)} 새 값" if name else "새 값",
            placeholder=f"현재 {s.original[name]:,}" if name else "숫자",
            max_length=12,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        s = self.panel.session
        name = self.name or self.field_input.value.strip()
        raw = self.value_input.value.strip().replace(",", "")
        if name not in s.original:
            await interaction.response.send_message(f"`{name}` 필드가 이 세이브에 없습니다.", ephemeral=True)
            return
        if not raw.isdigit():
            await interaction.response.send_message("숫자만 입력할 수 있습니다.", ephemeral=True)
            return
        val, cap = int(raw), cap_of(name)
        if val > cap:
            await interaction.response.send_message(
                f"{label_of(name)} 안전 상한은 {cap:,}입니다. 초과하면 게임이 종료될 수 있습니다.", ephemeral=True
            )
            return
        if val == s.original[name]:
            s.pending.pop(name, None)
        else:
            s.pending[name] = val
        await interaction.response.edit_message(embed=build_embed(s), view=self.panel)


class AmountModal(discord.ui.Modal):
    def __init__(self, panel: "PanelView", title: str, label: str, cap: int, desc: str, make):
        super().__init__(title=title)
        self.panel, self.cap, self.desc, self.make = panel, cap, desc, make
        self.amount = discord.ui.TextInput(label=label, placeholder=f"0 ~ {cap:,}", max_length=12)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip().replace(",", "")
        if not raw.isdigit() or int(raw) > self.cap:
            await interaction.response.send_message(f"0 ~ {self.cap:,} 사이 숫자만 가능합니다.", ephemeral=True)
            return
        v = int(raw)
        await queue_op(interaction, self.panel, self.desc.format(v=v), self.make(v))


class CatQueryModal(discord.ui.Modal):
    def __init__(self, panel: "PanelView", mode: str):
        titles = {"unlock": "유닛 해금", "remove": "유닛 삭제", "level": "유닛 레벨 설정"}
        super().__init__(title=titles[mode])
        self.panel, self.mode = panel, mode
        self.query = discord.ui.TextInput(label="유닛 ID 또는 이름", placeholder="예: 0 또는 냥코", max_length=40)
        self.add_item(self.query)
        if mode == "level":
            self.base = discord.ui.TextInput(label="기본 레벨", placeholder="유닛별 상한까지 자동 조정", max_length=3)
            self.plus = discord.ui.TextInput(label="+ 레벨", placeholder="0 ~ 90", max_length=3, default="0")
            self.add_item(self.base)
            self.add_item(self.plus)

    async def on_submit(self, interaction: discord.Interaction):
        q = self.query.value.strip()
        if self.mode == "unlock":
            await queue_op(interaction, self.panel, f"해금 `{q}`", partial(op_unlock_query, query=q))
        elif self.mode == "remove":
            await queue_op(interaction, self.panel, f"삭제 `{q}`", partial(op_remove_query, query=q))
        else:
            if not (self.base.value.isdigit() and self.plus.value.isdigit()):
                await interaction.response.send_message("레벨은 숫자만 가능합니다.", ephemeral=True)
                return
            b, p = int(self.base.value), min(int(self.plus.value), 90)
            await queue_op(interaction, self.panel, f"레벨 `{q}` → {b}+{p}",
                           partial(op_set_level, query=q, base=b, plus=p))


class StoryStageModal(discord.ui.Modal, title="스테이지 범위 클리어"):
    chapter = discord.ui.TextInput(label="챕터 번호", placeholder="예: 1", max_length=2)
    stages = discord.ui.TextInput(label="스테이지 (범위 가능)", placeholder="예: 1-10,15,20-48", max_length=60)

    def __init__(self, panel: "PanelView"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        total = story_chapter_count(self.panel.session.save_file)
        if not self.chapter.value.strip().isdigit() or not (1 <= int(self.chapter.value) <= total):
            await interaction.response.send_message(f"챕터는 1 ~ {total} 사이 숫자만 가능합니다.", ephemeral=True)
            return
        ch = int(self.chapter.value)
        picked = parse_ranges(self.stages.value, 48)
        if not picked:
            await interaction.response.send_message("형식이 올바르지 않습니다. 예: 1-10,15", ephemeral=True)
            return
        await queue_op(
            interaction, self.panel, f"스토리 {ch}챕터 스테이지 {len(picked)}개 클리어",
            partial(op_clear_story_stages, chapter=ch, stages=picked),
        )


class PresetNameModal(discord.ui.Modal, title="프리셋 저장"):
    preset_name = discord.ui.TextInput(label="프리셋 이름", max_length=30, placeholder="예: 기본 세팅")

    def __init__(self, panel: "PanelView"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        presets.save(interaction.user.id, self.preset_name.value.strip(), dict(self.panel.session.pending))
        await interaction.response.send_message(f"`{self.preset_name.value}` 저장했습니다.", ephemeral=True)


# ============================================================
# 컴포넌트
# ============================================================
class Btn(discord.ui.Button):
    def __init__(self, handler, label: str, style=discord.ButtonStyle.secondary, row: int | None = None):
        super().__init__(label=label, style=style, row=row)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)


class CategorySelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=name, value=key, description=desc, default=(key == current))
            for key, (name, desc) in CATEGORIES.items()
        ]
        super().__init__(placeholder="카테고리 선택", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        panel: PanelView = self.view
        panel.session.category = self.values[0]
        panel.rebuild()
        await interaction.response.edit_message(embed=build_embed(panel.session), view=panel)


class FieldSelect(discord.ui.Select):
    def __init__(self, session: Session):
        options = [
            discord.SelectOption(
                label=label_of(n), value=n,
                description=f"현재 {session.original[n]:,} / 상한 {cap_of(n):,}",
            )
            for n in PINNED if n in session.original
        ]
        options.append(discord.SelectOption(label="다른 필드 직접 입력", value="__custom__"))
        super().__init__(placeholder="변경할 항목 선택", options=options[:25], row=1)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        await interaction.response.send_modal(ValueModal(self.view, None if choice == "__custom__" else choice))


class RaritySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="희귀도별 전체 해금",
            options=[discord.SelectOption(label=v, value=str(k)) for k, v in RARITIES.items()],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        r = int(self.values[0])
        await queue_op(interaction, self.view, f"{RARITIES[r]} 전체 해금", partial(op_unlock_rarity, rarity=r))


class ChapterSelect(discord.ui.Select):
    def __init__(self, total: int):
        super().__init__(
            placeholder="클리어할 스토리 챕터 (복수 선택)",
            min_values=1,
            max_values=total,
            options=[discord.SelectOption(label=f"스토리 {i}챕터", value=str(i)) for i in range(1, total + 1)],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        picked = sorted(int(v) for v in self.values)
        await queue_op(
            interaction, self.view, f"스토리 {','.join(map(str, picked))}챕터 클리어",
            partial(op_clear_story_chapters, chapters=picked),
        )


class PresetPicker(discord.ui.View):
    def __init__(self, panel: "PanelView", saved: dict[str, dict[str, int]]):
        super().__init__(timeout=120)
        self.panel, self.saved = panel, saved
        sel = discord.ui.Select(
            placeholder="불러올 프리셋",
            options=[discord.SelectOption(label=n[:100], value=n[:100], description=f"{len(v)}개 항목")
                     for n, v in list(saved.items())[:25]],
        )
        sel.callback = self.on_pick
        self.add_item(sel)
        self.sel = sel

    async def on_pick(self, interaction: discord.Interaction):
        s = self.panel.session
        applied = skipped = 0
        for name, val in self.saved.get(self.sel.values[0], {}).items():
            if name in s.original and 0 <= val <= cap_of(name):
                if val == s.original[name]:
                    s.pending.pop(name, None)
                else:
                    s.pending[name] = val
                applied += 1
            else:
                skipped += 1
        await interaction.response.edit_message(content=f"{applied}개 적용, {skipped}개 건너뜀.", view=None)
        await refresh_panel(self.panel, f"프리셋 적용: {applied}개 (건너뜀 {skipped}개)")


# ============================================================
# 메인 패널
# ============================================================
class PanelView(discord.ui.View):
    def __init__(self, bot: "AxiomBot", user_id: int, session: Session):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.bot, self.user_id, self.session = bot, user_id, session
        self.message: discord.WebhookMessage | None = None
        session.active = self
        session.touched = time.time()
        self.rebuild()

    def q(self, desc: str, fn):
        async def handler(interaction: discord.Interaction):
            await queue_op(interaction, self, desc, fn)
        return handler

    def modal(self, factory):
        async def handler(interaction: discord.Interaction):
            await interaction.response.send_modal(factory())
        return handler

    def rebuild(self) -> None:
        self.clear_items()
        s = self.session
        P, S, G, R = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        self.add_item(CategorySelect(s.category))

        if s.category == "currency":
            self.add_item(FieldSelect(s))

        elif s.category == "cats":
            self.add_item(RaritySelect())
            self.add_item(Btn(self.q("전체 해금(획득 가능)", op_unlock_all), "전체 해금", P, 2))
            self.add_item(Btn(self.q("보유 유닛 3진화", op_true_forms), "3진 전체", P, 2))
            self.add_item(Btn(self.q("보유 유닛 4진화", op_fourth_forms), "4진 전체", P, 2))
            self.add_item(Btn(self.q("보유 유닛 레벨 30+0", partial(op_level_all_unlocked, base=30, plus=0)),
                              "레벨 30", S, 2))
            self.add_item(Btn(self.modal(lambda: CatQueryModal(self, "unlock")), "이름/ID 해금", G, 3))
            self.add_item(Btn(self.modal(lambda: CatQueryModal(self, "level")), "이름/ID 레벨", G, 3))
            self.add_item(Btn(self.modal(lambda: CatQueryModal(self, "remove")), "이름/ID 삭제", R, 3))

        elif s.category == "stages":
            total = min(story_chapter_count(s.save_file), 25)
            if total > 0:
                self.add_item(ChapterSelect(total))
            self.add_item(Btn(self.modal(lambda: StoryStageModal(self)), "스테이지 지정", G, 2))
            self.add_item(Btn(self.q("스토리 전체 클리어", op_clear_story), "스토리 전체", P, 2))
            self.add_item(Btn(self.q("보물 전부 3단계", partial(op_clear_treasures, level=3)), "보물 3단계", P, 2))
            self.add_item(Btn(self.q("레전드 스토리 클리어", op_clear_legend), "레전드", P, 3))
            self.add_item(Btn(self.q("제로 레전드 클리어", op_clear_zero_legends), "제로 레전드", P, 3))
            self.add_item(Btn(self.q("이벤트 스테이지 클리어", op_clear_events), "이벤트", P, 3))

        elif s.category == "gamatoto":
            self.add_item(Btn(
                self.modal(lambda: AmountModal(
                    self, "가마토토 XP", "XP", 99_999_999, "가마토토 XP → {v:,}",
                    lambda v: partial(op_gamatoto_xp, value=v))),
                "가마토토 XP", P, 1))
            self.add_item(Btn(
                self.modal(lambda: AmountModal(
                    self, "마타타비 열매", "수량", 999, "마타타비 열매 → {v}",
                    lambda v: partial(op_set_all_items, path="catfruit", value=v))),
                "마타타비 열매", P, 1))
            self.add_item(Btn(
                self.modal(lambda: AmountModal(
                    self, "캣아이즈", "수량", 999, "캣아이즈 → {v}",
                    lambda v: partial(op_set_all_items, path="catseyes", value=v))),
                "캣아이즈", P, 1))
            self.add_item(Btn(
                self.modal(lambda: AmountModal(
                    self, "캣아미", "수량", 999, "캣아미 → {v}",
                    lambda v: partial(op_set_all_items, path="catamins.catamins", value=v))),
                "캣아미", P, 1))

        elif s.category == "misc":
            self.add_item(Btn(self.h_save_preset, "프리셋 저장", S, 1))
            self.add_item(Btn(self.h_load_preset, "프리셋 불러오기", S, 1))
            self.add_item(Btn(self.h_fields, "필드 목록", S, 1))
            self.add_item(Btn(self.h_toggle, "새 계정 옵션", S, 1))

        self.add_item(Btn(self.h_apply, "저장 · 코드 발급", G, 4))
        self.add_item(Btn(self.h_preview, "변경 확인", P, 4))
        self.add_item(Btn(self.h_undo, "마지막 작업 취소", S, 4))
        self.add_item(Btn(self.h_reset, "전체 초기화", S, 4))
        self.add_item(Btn(self.h_close, "닫기", R, 4))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인 세션이 아닙니다.", ephemeral=True)
            return False
        self.session.touched = time.time()
        if self.session.busy:
            await interaction.response.send_message("처리 중입니다. 잠시만 기다려 주세요.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.session.active is self and time.time() - self.session.touched >= SESSION_TIMEOUT - 1:
            self.bot.sessions.pop(self.user_id, None)
        if self.message:
            try:
                await self.message.edit(
                    content=None,
                    embed=discord.Embed(title="세션 만료", description="에디터 패널에서 다시 시작하세요.", color=Theme.ERR),
                    view=None,
                )
            except Exception:
                pass

    async def _update(self, interaction: discord.Interaction, note: str = "") -> None:
        await interaction.response.edit_message(embed=build_embed(self.session, note), view=self)

    async def h_catfood(self, interaction: discord.Interaction):
        s = self.session
        if "catfood" not in s.original:
            await interaction.response.send_message("catfood 필드가 없습니다.", ephemeral=True)
            return
        if s.original["catfood"] == 45000:
            s.pending.pop("catfood", None)
        else:
            s.pending["catfood"] = 45000
        await self._update(interaction)

    async def h_tickets(self, interaction: discord.Interaction):
        s, hit = self.session, 0
        for name, cur in s.original.items():
            if "ticket" in name.lower():
                hit += 1
                if cur == 999:
                    s.pending.pop(name, None)
                else:
                    s.pending[name] = 999
        await self._update(interaction, f"티켓 필드 {hit}개를 999로 설정")

    async def h_fields(self, interaction: discord.Interaction):
        text = "\n".join(f"{n} = {v}" for n, v in sorted(self.session.original.items()))
        if len(text) > 1850:
            text = text[:1850] + "\n... (생략)"
        await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)

    async def h_toggle(self, interaction: discord.Interaction):
        self.session.new_account = not self.session.new_account
        await self._update(interaction)

    async def h_preview(self, interaction: discord.Interaction):
        s = self.session
        lines = [f"{label_of(k)}: {s.original[k]:,} → {v:,}" for k, v in s.pending.items()]
        lines += [f"작업 {i + 1}: {d}" for i, (d, _) in enumerate(s.ops)]
        text = "\n".join(lines) or "변경 사항이 없습니다."
        if len(text) > 1850:
            text = text[:1850] + "\n... (생략)"
        await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)

    async def h_undo(self, interaction: discord.Interaction):
        if not self.session.ops:
            await interaction.response.send_message("취소할 작업이 없습니다.", ephemeral=True)
            return
        desc, _ = self.session.ops.pop()
        await self._update(interaction, f"작업 취소: {desc}")

    async def h_reset(self, interaction: discord.Interaction):
        self.session.pending.clear()
        self.session.ops.clear()
        await self._update(interaction, "수치와 작업을 모두 초기화했습니다.")

    async def h_close(self, interaction: discord.Interaction):
        self.bot.sessions.pop(self.user_id, None)
        self.stop()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="세션 종료", description="서버에는 아무것도 반영되지 않았습니다.", color=Theme.BUSY),
            view=None,
        )

    async def h_save_preset(self, interaction: discord.Interaction):
        if not self.session.pending:
            await interaction.response.send_message("저장할 수치 변경이 없습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(PresetNameModal(self))

    async def h_load_preset(self, interaction: discord.Interaction):
        saved = presets.list(interaction.user.id)
        if not saved:
            await interaction.response.send_message("저장된 프리셋이 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message("불러올 프리셋을 고르세요.", view=PresetPicker(self, saved), ephemeral=True)

    async def h_apply(self, interaction: discord.Interaction):
        s = self.session
        if not s.pending and not s.ops:
            await interaction.response.send_message("변경 사항이 없습니다.", ephemeral=True)
            return

        s.busy = True
        await interaction.response.edit_message(embed=busy_embed(), view=None)
        loop = asyncio.get_running_loop()
        codes, err, results = await loop.run_in_executor(
            None, apply_and_upload, s.save_file, dict(s.pending), s.new_account, list(s.ops)
        )
        s.busy = False

        if codes is None:
            await interaction.edit_original_response(embed=error_embed(err), view=self)
            return

        self.bot.sessions.pop(self.user_id, None)
        self.stop()

        card = code_embed(codes)
        dm_ok = await send_dm(interaction.user, embeds=[card])
        if dm_ok:
            notice = discord.Embed(title="완료", description="이어하기 코드와 비밀번호를 DM으로 보냈습니다.", color=Theme.OK)
            fails = [r for r in results if "실패" in r]
            if fails:
                notice.add_field(name="실패한 작업", value="\n".join(fails)[:1000], inline=False)
            await interaction.edit_original_response(embed=notice, view=None)
        else:
            card.description = "DM을 보낼 수 없어 여기에 표시합니다. 지금 복사해 두세요."
            await interaction.edit_original_response(embed=card, view=None)


# ============================================================
# 랜계 밸런스 패널 (관리자)
# ============================================================
def balance_embed(tier_key: str, note: str = "") -> discord.Embed:
    name, p = TIERS[tier_key]
    e = discord.Embed(title=f"랜계 밸런스 · {name}", description=note or None, color=Theme.ACCENT)
    for key, (label, kind, _, _) in PARAMS.items():
        v = p[key]
        if kind == "weights":
            txt = " / ".join(f"{RARITIES[i]} {w:g}" for i, w in enumerate(v))
        elif kind == "float":
            txt = f"`{v[0]:.2f}` ~ `{v[1]:.2f}`"
        else:
            txt = f"`{v[0]:,}` ~ `{v[1]:,}`"
        e.add_field(name=label, value=txt, inline=(kind != "weights"))
    e.add_field(
        name="티켓 상한",
        value=" · ".join(f"{k} {TICKET_CAPS.get(k, 0)}" for k in TICKET_ORDER),
        inline=False,
    )
    e.add_field(
        name="희귀도별 레벨 배율",
        value=" · ".join(f"{RARITIES[r]} x{RARITY_LEVEL_MULT[r]:g}" for r in RARITIES),
        inline=False,
    )
    return e


class ParamModal(discord.ui.Modal):
    def __init__(self, panel: "BalancePanel", key: str):
        label, kind, lo, hi = PARAMS[key]
        super().__init__(title=f"{label} 수정")
        self.panel, self.key, self.kind, self.lo, self.hi = panel, key, kind, lo, hi
        cur = TIERS[panel.tier][1][key]
        if kind == "weights":
            self.a = discord.ui.TextInput(
                label="가중치 6개 (일반,EX,레어,슈퍼,울트라,레전드)",
                default=",".join(f"{w:g}" for w in cur), max_length=60)
            self.add_item(self.a)
        else:
            self.a = discord.ui.TextInput(label="최소 (진행도 0일 때)", default=f"{cur[0]:g}", max_length=10)
            self.b = discord.ui.TextInput(label="최대 (진행도 1일 때)", default=f"{cur[1]:g}", max_length=10)
            self.add_item(self.a)
            self.add_item(self.b)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if self.kind == "weights":
                vals = tuple(float(x) for x in self.a.value.replace(" ", "").split(","))
                if len(vals) != 6 or any(not (0 <= x <= self.hi) for x in vals) or sum(vals) <= 0:
                    raise ValueError
            else:
                cast = float if self.kind == "float" else int
                vals = (cast(self.a.value), cast(self.b.value))
                if not (self.lo <= vals[0] <= vals[1] <= self.hi):
                    raise ValueError
        except ValueError:
            await interaction.response.send_message(
                f"값이 올바르지 않습니다. 범위 {self.lo} ~ {self.hi}, 최소 ≤ 최대, 가중치는 6개이며 합이 0보다 커야 합니다.",
                ephemeral=True)
            return
        TIERS[self.panel.tier][1][self.key] = vals
        save_balance()
        await interaction.response.edit_message(embed=balance_embed(self.panel.tier, "저장했습니다."), view=self.panel)


class TicketModal(discord.ui.Modal, title="티켓 상한 (진행도 1일 때)"):
    caps = discord.ui.TextInput(label="normal,rare,gold,platinum,legend,lucky", max_length=40)

    def __init__(self, panel: "BalancePanel"):
        super().__init__()
        self.panel = panel
        self.caps.default = ",".join(str(TICKET_CAPS.get(k, 0)) for k in TICKET_ORDER)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            vals = [int(x) for x in self.caps.value.replace(" ", "").split(",")]
            if len(vals) != 6 or any(not (0 <= x <= 999) for x in vals):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("0~999 숫자 6개를 쉼표로 입력하세요.", ephemeral=True)
            return
        TICKET_CAPS.update(dict(zip(TICKET_ORDER, vals)))
        save_balance()
        await interaction.response.edit_message(embed=balance_embed(self.panel.tier, "저장했습니다."), view=self.panel)


class LevelMultModal(discord.ui.Modal, title="희귀도별 레벨 배율"):
    mult = discord.ui.TextInput(label="일반,EX,레어,슈퍼,울트라,레전드 (0.1~2)", max_length=40)

    def __init__(self, panel: "BalancePanel"):
        super().__init__()
        self.panel = panel
        self.mult.default = ",".join(f"{RARITY_LEVEL_MULT[r]:g}" for r in RARITIES)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            vals = [float(x) for x in self.mult.value.replace(" ", "").split(",")]
            if len(vals) != 6 or any(not (0.1 <= x <= 2) for x in vals):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("0.1~2 숫자 6개를 쉼표로 입력하세요.", ephemeral=True)
            return
        RARITY_LEVEL_MULT.update(dict(zip(RARITIES, vals)))
        save_balance()
        await interaction.response.edit_message(embed=balance_embed(self.panel.tier, "저장했습니다."), view=self.panel)


class BalancePanel(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.tier = "basic"
        self._build()

    @staticmethod
    def _btn(label, handler, row, style=discord.ButtonStyle.secondary):
        b = discord.ui.Button(label=label, style=style, row=row)
        b.callback = handler
        return b

    def _build(self) -> None:
        self.clear_items()
        tier_sel = discord.ui.Select(
            placeholder="등급 선택",
            options=[discord.SelectOption(label=v[0], value=k, default=(k == self.tier)) for k, v in TIERS.items()],
            row=0,
        )
        tier_sel.callback = self.on_tier
        param_sel = discord.ui.Select(
            placeholder="수정할 항목 선택",
            options=[discord.SelectOption(label=v[0], value=k) for k, v in PARAMS.items()],
            row=1,
        )
        param_sel.callback = self.on_param
        self.add_item(tier_sel)
        self.add_item(param_sel)
        self.add_item(self._btn("티켓 상한", self.h_tickets, 2))
        self.add_item(self._btn("레벨 배율", self.h_mult, 2))
        self.add_item(self._btn("시뮬레이션", self.h_preview, 2, discord.ButtonStyle.primary))
        self.add_item(self._btn("이 등급 기본값", self.h_reset, 3, discord.ButtonStyle.danger))
        self.add_item(self._btn("닫기", self.h_close, 3))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ADMIN_IDS or interaction.user.id != self.user_id:
            await interaction.response.send_message("관리자 전용입니다.", ephemeral=True)
            return False
        return True

    async def on_tier(self, interaction: discord.Interaction):
        self.tier = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(embed=balance_embed(self.tier), view=self)

    async def on_param(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ParamModal(self, interaction.data["values"][0]))

    async def h_tickets(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketModal(self))

    async def h_mult(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LevelMultModal(self))

    async def h_preview(self, interaction: discord.Interaction):
        r = simulate(self.tier)
        lines = [f"**{k}**  최소 `{lo:,}` · 평균 `{av:,}` · 최대 `{hi:,}`" for k, (lo, av, hi) in r.items()]
        e = balance_embed(self.tier, "200회 시뮬레이션 (서버 사용 안 함)\n" + "\n".join(lines))
        await interaction.response.edit_message(embed=e, view=self)

    async def h_reset(self, interaction: discord.Interaction):
        TIERS[self.tier][1].clear()
        TIERS[self.tier][1].update(copy.deepcopy(DEFAULT_TIERS[self.tier][1]))
        save_balance()
        await interaction.response.edit_message(embed=balance_embed(self.tier, "기본값으로 복원했습니다."), view=self)

    async def h_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="밸런스 패널 종료", color=Theme.BUSY), view=None)


# ============================================================
# 보안 패널 (관리자)
# ============================================================
def sec_embed(note: str = "") -> discord.Embed:
    on = lambda v: "켜짐" if v else "꺼짐"
    e = discord.Embed(title="보안 설정", description=note or None, color=Theme.ACCENT)
    e.add_field(name="허용 유저 제한", value=on(SEC["allowlist_on"]), inline=True)
    e.add_field(name="랜덤계정 관리자 전용", value=on(SEC["create_admin_only"]), inline=True)
    e.add_field(name="사용 기록", value=on(SEC["log_on"]), inline=True)
    e.add_field(name="쿨타임", value=f"{SEC['cooldown']}초" if SEC["cooldown"] else "없음", inline=True)
    e.add_field(name="허용 목록", value=" ".join(f"<@{u}>" for u in SEC["allowed"][:20]) or "비어 있음", inline=False)
    e.add_field(name="차단 목록", value=" ".join(f"<@{u}>" for u in SEC["blocked"][:20]) or "비어 있음", inline=False)
    e.set_footer(text="관리자는 모든 제한을 받지 않습니다")
    return e


class UserIdModal(discord.ui.Modal):
    def __init__(self, panel: "SecurityPanel", list_key: str, add: bool):
        names = {"allowed": "허용", "blocked": "차단"}
        super().__init__(title=f"{names[list_key]} 목록 {'추가' if add else '제거'}")
        self.panel, self.list_key, self.add = panel, list_key, add
        self.uid = discord.ui.TextInput(label="유저 ID (숫자)", placeholder="프로필 우클릭 → 사용자 ID 복사", max_length=25)
        self.add_item(self.uid)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.uid.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("숫자 ID만 입력하세요.", ephemeral=True)
            return
        uid, lst = int(raw), SEC[self.list_key]
        if self.add and uid not in lst:
            lst.append(uid)
        elif not self.add and uid in lst:
            lst.remove(uid)
        save_sec()
        await interaction.response.edit_message(embed=sec_embed("저장했습니다."), view=self.panel)


class CooldownModal(discord.ui.Modal, title="쿨타임 설정"):
    sec = discord.ui.TextInput(label="초 (0이면 없음)", placeholder="예: 60", max_length=5)

    def __init__(self, panel: "SecurityPanel"):
        super().__init__()
        self.panel = panel
        self.sec.default = str(SEC["cooldown"])

    async def on_submit(self, interaction: discord.Interaction):
        if not self.sec.value.strip().isdigit():
            await interaction.response.send_message("숫자만 입력하세요.", ephemeral=True)
            return
        SEC["cooldown"] = min(int(self.sec.value), 86400)
        save_sec()
        await interaction.response.edit_message(embed=sec_embed("저장했습니다."), view=self.panel)


class SecurityPanel(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger

        async def modal_h(factory, interaction: discord.Interaction):
            await interaction.response.send_modal(factory())

        items = [
            ("허용 제한 토글", self.h_allow, 0, S),
            ("랜덤계정 관리자 전용", self.h_admin_only, 0, S),
            ("기록 토글", self.h_log, 0, S),
            ("허용 추가", partial(modal_h, lambda: UserIdModal(self, "allowed", True)), 1, S),
            ("허용 제거", partial(modal_h, lambda: UserIdModal(self, "allowed", False)), 1, S),
            ("차단", partial(modal_h, lambda: UserIdModal(self, "blocked", True)), 1, D),
            ("차단 해제", partial(modal_h, lambda: UserIdModal(self, "blocked", False)), 1, S),
            ("쿨타임", partial(modal_h, lambda: CooldownModal(self)), 2, S),
            ("최근 기록", self.h_logs, 2, discord.ButtonStyle.primary),
            ("닫기", self.h_close, 2, D),
        ]
        for label, handler, row, style in items:
            b = discord.ui.Button(label=label, style=style, row=row)
            b.callback = handler
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ADMIN_IDS or interaction.user.id != self.user_id:
            await interaction.response.send_message("관리자 전용입니다.", ephemeral=True)
            return False
        return True

    async def h_allow(self, interaction: discord.Interaction):
        SEC["allowlist_on"] = not SEC["allowlist_on"]
        save_sec()
        await interaction.response.edit_message(embed=sec_embed("저장했습니다."), view=self)

    async def h_admin_only(self, interaction: discord.Interaction):
        SEC["create_admin_only"] = not SEC["create_admin_only"]
        save_sec()
        await interaction.response.edit_message(embed=sec_embed("저장했습니다."), view=self)

    async def h_log(self, interaction: discord.Interaction):
        SEC["log_on"] = not SEC["log_on"]
        save_sec()
        await interaction.response.edit_message(embed=sec_embed("저장했습니다."), view=self)

    async def h_logs(self, interaction: discord.Interaction):
        try:
            lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-15:]
        except OSError:
            lines = []
        rows = []
        for ln in lines:
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            when, uid, what = parts
            rows.append(f"`{when[5:16]}` <@{uid}> 님이 **{what}**")
        e = discord.Embed(title="최근 사용 기록", description="\n".join(rows) or "기록이 없습니다.", color=Theme.ACCENT)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def h_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(title="보안 패널 종료", color=Theme.BUSY), view=None)


# ============================================================
# 빠른충전 패널 (통조림 45,000 고정)
# ============================================================
FREE_FILE = BASE_DIR / "freegen.json"
STOCK_FILE = BASE_DIR / "stock.json"
FREE: dict = {"cooldown_hours": 24, "total": 0, "last": {}, "gen_last": {}, "gen_total": 0, "need_review": []}
STOCK: dict = {"basic": [], "mid": [], "high": []}
FREE_BUSY: set[int] = set()
REVIEW_MSG = "이용 후기를 먼저 작성해주세요. 후기 패널의 '후기 작성하기' 버튼을 눌러 남기면 다시 이용할 수 있어요."


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_free() -> None:
    try:
        FREE.update(json.loads(FREE_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        STOCK.update(json.loads(STOCK_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass


def save_free() -> None:
    _atomic_write(FREE_FILE, FREE)


def save_stock() -> None:
    _atomic_write(STOCK_FILE, STOCK)


def free_ready_at(uid: int) -> float:
    """다시 받을 수 있는 시각 (이미 가능하면 0)."""
    if uid in ADMIN_IDS or FREE["cooldown_hours"] <= 0:
        return 0.0
    nxt = FREE["last"].get(str(uid), 0) + FREE["cooldown_hours"] * 3600
    return nxt if nxt > time.time() else 0.0


def review_blocked(guild_id: int | None, uid: int) -> bool:
    if guild_id is None or uid in ADMIN_IDS:
        return False
    return bool(gsec(guild_id, "free")["review_required"] and str(uid) in FREE["need_review"])


def mark_need_review(uid: int) -> None:
    if str(uid) not in FREE["need_review"]:
        FREE["need_review"].append(str(uid))
    save_free()


async def use_log(guild: discord.Guild | None, user: discord.abc.User, text: str) -> None:
    if guild is None:
        return
    e = discord.Embed(description=f"{user.mention} 님이 **{text}**", color=Theme.ACCENT, timestamp=discord.utils.utcnow())
    e.set_author(name="이용 로그", icon_url=user.display_avatar.url)
    await send_log(guild, gsec(guild.id, "free")["log_channel"], e)


def free_embed() -> discord.Embed:
    cd = FREE["cooldown_hours"]
    if cd <= 0:
        rule = "제한 없이 받을 수 있습니다."
    elif cd >= 24 * 365:
        rule = "1인당 1회만 받을 수 있습니다."
    else:
        rule = f"{cd}시간에 1회 받을 수 있습니다."
    e = discord.Embed(
        title="무료 통조림",
        description=(
            "아래 받기 버튼을 눌러 혜택을 받아보세요.\n"
            "자동으로 무료통조림을 드립니다.\n"
            "만약 안될시 문의주시면 해결해드리겠습니다.\n\n"
            f"1. 무료통조림은 {rule}\n"
            "2. 티켓으로 드리는게 아닌 자동으로 봇이 드립니다. 코드를 잘 보관해주세요.\n"
            "3. 부계정 사용을 금지합니다"
        ),
        color=Theme.MAIN,
    )
    e.set_author(name=f"{SHOP_NAME} · FREE GIFT")
    e.add_field(
        name="⚠️ 경고사항",
        value="코드 등록 후 **'기종변경'** 종류를 누르면 **계정이 날아갑니다.**\n절대 누르지 마세요.",
        inline=False,
    )
    return e


class QuickModal(discord.ui.Modal, title="무료 통조림"):
    transfer_code = discord.ui.TextInput(label="이어하기 코드", placeholder="예: 1a2b3c4d5", min_length=4, max_length=30)
    confirmation_code = discord.ui.TextInput(label="비밀번호", placeholder="4자리", min_length=4, max_length=20)

    def __init__(self, country: str):
        super().__init__()
        self.country = country

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if uid in FREE_BUSY:
            await interaction.response.send_message("이미 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)
            return
        if review_blocked(interaction.guild_id, uid):
            await interaction.response.send_message(embed=error_embed(REVIEW_MSG), ephemeral=True)
            return
        ready = free_ready_at(uid)
        if ready:
            await interaction.response.send_message(
                embed=error_embed(f"아직 받을 수 없습니다. <t:{int(ready)}:R> 다시 가능합니다."), ephemeral=True)
            return

        FREE_BUSY.add(uid)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            msg = await interaction.followup.send(embed=busy_embed("계정을 불러오는 중", 0, 3), ephemeral=True, wait=True)

            async def step(text: str, n: int):
                try:
                    await msg.edit(embed=busy_embed(text, n, 3))
                except discord.HTTPException:
                    pass

            loop = asyncio.get_running_loop()
            sf, err = await loop.run_in_executor(
                None, download_save,
                self.transfer_code.value.strip(), self.confirmation_code.value.strip(), self.country,
            )
            if sf is None:
                await msg.edit(embed=error_embed(err))
                return
            await step("원본을 백업하는 중", 1)
            await loop.run_in_executor(None, backup_save, sf, uid)
            await step(f"통조림 {QUICK_CATFOOD:,}개를 지급하는 중", 2)
            codes, err, _ = await loop.run_in_executor(
                None, apply_and_upload, sf, {"catfood": QUICK_CATFOOD}, True, []
            )
            if codes is None:
                await msg.edit(embed=error_embed(err))
                return

            FREE["last"][str(uid)] = time.time()
            FREE["total"] += 1
            mark_need_review(uid)

            card = code_embed(codes, f"통조림 {QUICK_CATFOOD:,}개 지급 완료")
            card.set_footer(text="⚠️ 코드 등록 후 '기종변경' 종류를 누르면 계정이 날아갑니다")
            if await send_dm(interaction.user, embeds=[card]):
                await msg.edit(embed=discord.Embed(
                    title="✅ 지급 완료",
                    description=f"통조림 `{QUICK_CATFOOD:,}개`를 지급했습니다.\nDM을 확인해주세요. 이어하기 코드와 비밀번호를 보냈어요!",
                    color=Theme.OK))
            else:
                card.description = "DM을 보낼 수 없어 여기에 표시합니다. 지금 복사해 두세요."
                await msg.edit(embed=card)
            await use_log(interaction.guild, interaction.user, "무료통조림 수령")
        finally:
            FREE_BUSY.discard(uid)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 무료통조림 오류: {type(error).__name__}", file=sys.stderr)


# ============================================================
# 로그인 / 봇 / 명령어
# ============================================================
class LoginModal(discord.ui.Modal, title="계정 연결"):
    transfer_code = discord.ui.TextInput(label="이어하기 코드", placeholder="예: 1a2b3c4d5", min_length=4, max_length=30)
    confirmation_code = discord.ui.TextInput(label="비밀번호", placeholder="4자리", min_length=4, max_length=20)

    def __init__(self, bot: "AxiomBot", country: str, category: str = "home"):
        super().__init__()
        self.bot, self.country, self.category = bot, country, category

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()
        save_file, err = await loop.run_in_executor(
            None, download_save,
            self.transfer_code.value.strip(), self.confirmation_code.value.strip(), self.country,
        )
        if save_file is None:
            await interaction.followup.send(embed=error_embed(err), ephemeral=True)
            return

        backup_name = await loop.run_in_executor(None, backup_save, save_file, uid)
        session = Session(save_file=save_file, original=scan_int_fields(save_file),
                          country=self.country, category=self.category, backup=backup_name)
        self.bot.sessions[uid] = session
        if self.category == "reg":
            session.category = "home"
            await interaction.followup.send(embed=discord.Embed(
                title="계정 등록 완료",
                description=("패널에서 재화 · 고양이 · 스테이지 · 가마토토 · 기타 중에 골라서 편집하세요.\n"
                             f"{SESSION_TIMEOUT // 60}분 동안 조작이 없으면 등록이 풀려요."),
                color=Theme.OK), ephemeral=True)
            return
        panel = PanelView(self.bot, uid, session)
        panel.message = await interaction.followup.send(embed=build_embed(session), view=panel, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 모달 오류: {type(error).__name__}", file=sys.stderr)


# ============================================================
# 무통 허브 패널 (공개 · 카테고리 셀렉트)
# ============================================================
def editor_embed() -> discord.Embed:
    e = discord.Embed(
        title=f"{SHOP_NAME} 에디터",
        description=("1. **계정 등록**을 눌러 이어하기 코드와 비밀번호를 먼저 등록하세요.\n"
                     "2. 등록하고 나면 아래에서 카테고리를 골라 편집하세요."),
        color=Theme.ACCENT,
    )
    e.add_field(name="⚠️ 경고사항", value="코드 등록 후 **'기종변경'** 종류를 누르면 **계정이 날아갑니다.**\n절대 누르지 마세요.", inline=False)
    return e


EDITOR_MENU = [
    ("재화", "currency"),
    ("고양이", "cats"),
    ("스테이지", "stages"),
    ("가마토토", "gamatoto"),
    ("기타", "misc"),
]


class EditorHub(discord.ui.View):
    """재시작해도 살아있는 에디터 고정 패널. 계정 등록 → 카테고리 선택 순서."""

    def __init__(self, country: str):
        super().__init__(timeout=None)
        self.country = country
        reg = discord.ui.Button(label="계정 등록", style=discord.ButtonStyle.success,
                                custom_id=f"editor_reg:{country}", row=0)
        reg.callback = self.on_register
        sel = discord.ui.Select(
            placeholder="카테고리를 선택하세요",
            custom_id=f"editor:{country}",
            options=[discord.SelectOption(label=n, value=k) for n, k in EDITOR_MENU],
            row=1,
        )
        sel.callback = self.on_pick
        self.add_item(reg)
        self.add_item(sel)

    async def _reset(self, interaction: discord.Interaction):
        try:
            if interaction.message:
                await interaction.message.edit(view=EditorHub(self.country))
        except discord.HTTPException:
            pass

    async def on_register(self, interaction: discord.Interaction):
        if await guard(interaction, "menu"):
            await interaction.response.send_modal(LoginModal(bot, self.country, "reg"))

    async def on_pick(self, interaction: discord.Interaction):
        cat = interaction.data["values"][0]
        uid = interaction.user.id
        sess = bot.sessions.get(uid)
        if sess is not None and time.time() - sess.touched > SESSION_TIMEOUT:
            bot.sessions.pop(uid, None)
            sess = None
        if sess is None:
            await interaction.response.send_message(
                embed=error_embed("먼저 **계정 등록** 버튼으로 계정을 등록해주세요."), ephemeral=True)
        else:
            sess.category = cat
            panel = PanelView(bot, uid, sess)
            await interaction.response.send_message(embed=build_embed(sess), view=panel, ephemeral=True)
            panel.message = await interaction.original_response()
        await self._reset(interaction)


class QuickView(discord.ui.View):
    """재시작해도 버튼이 살아있는 무료통조림 패널."""

    def __init__(self, country: str):
        super().__init__(timeout=None)
        self.country = country
        get = discord.ui.Button(label="무료통조림 받기", style=discord.ButtonStyle.success, custom_id=f"quick:{country}")
        get.callback = self.on_click
        self.add_item(get)

    async def on_click(self, interaction: discord.Interaction):
        if not await guard(interaction, "quick"):
            return
        if review_blocked(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(embed=error_embed(REVIEW_MSG), ephemeral=True)
            return
        ready = free_ready_at(interaction.user.id)
        if ready:
            await interaction.response.send_message(
                embed=error_embed(f"아직 받을 수 없습니다. <t:{int(ready)}:R> 다시 가능합니다."), ephemeral=True)
            return
        await interaction.response.send_modal(QuickModal(self.country))


# ============================================================
# 서버 설정 저장소 (티켓 / 인증)
# ============================================================
GCFG_FILE = BASE_DIR / "guilds.json"
GCFG: dict = {}
DEFAULTS = {
    "ticket": {"category": 0, "log": 0, "support": 0, "max_per_user": 1, "counter": 0,
               "types": ["일반 문의", "구매 문의", "신고"]},
    "verify": {"role": 0, "remove_role": 0, "log": 0, "mode": "button", "min_age": 0},
    "panels": {},
    "free": {"gen_channel": 0, "cat_channel": 0, "log_channel": 0, "review_channel": 0,
             "review_required": False, "gen_cooldown_hours": 24, "weights": [80, 19, 1]},
}


def load_gcfg() -> None:
    try:
        GCFG.update(json.loads(GCFG_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass


def save_gcfg() -> None:
    tmp = GCFG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(GCFG, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GCFG_FILE)


def gsec(gid: int, section: str) -> dict:
    sec = GCFG.setdefault(str(gid), {}).setdefault(section, {})
    for k, v in DEFAULTS[section].items():
        sec.setdefault(k, copy.deepcopy(v))
    return sec


def is_manager(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ADMIN_IDS:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


def mention(kind: str, _id: int) -> str:
    if not _id:
        return "미설정"
    return {"c": f"<#{_id}>", "r": f"<@&{_id}>"}[kind]


async def send_log(guild: discord.Guild, channel_id: int, embed: discord.Embed) -> None:
    if not channel_id:
        return
    ch = guild.get_channel(channel_id)
    if ch is None:
        return
    try:
        await ch.send(embed=embed)
    except discord.HTTPException:
        pass


class CfgChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, section: str, key: str, placeholder: str, types, row: int, gid: int = 0):
        cur = gsec(gid, section)[key] if gid else 0
        kw = {"default_values": [discord.Object(id=cur, type=discord.abc.GuildChannel)]} if cur else {}
        super().__init__(placeholder=placeholder, channel_types=types, min_values=1, max_values=1, row=row, **kw)
        self.section, self.key = section, key

    async def callback(self, interaction: discord.Interaction):
        try:
            picked = self.values[0].id
            gsec(interaction.guild_id, self.section)[self.key] = picked
            save_gcfg()
            self.default_values = [discord.Object(id=picked, type=discord.abc.GuildChannel)]
            await self.view.refresh(interaction)
        except Exception as e:
            print(f"[AXIOM] 설정 저장 오류: {type(e).__name__}: {e}", file=sys.stderr)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed(f"저장하지 못했어요 ({type(e).__name__})"), ephemeral=True)


class CfgRoleSelect(discord.ui.RoleSelect):
    def __init__(self, section: str, key: str, placeholder: str, row: int, gid: int = 0):
        cur = gsec(gid, section)[key] if gid else 0
        kw = {"default_values": [discord.Object(id=cur, type=discord.Role)]} if cur else {}
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row, **kw)
        self.section, self.key = section, key

    async def callback(self, interaction: discord.Interaction):
        try:
            picked = self.values[0].id
            gsec(interaction.guild_id, self.section)[self.key] = picked
            save_gcfg()
            self.default_values = [discord.Object(id=picked, type=discord.Role)]
            await self.view.refresh(interaction)
        except Exception as e:
            print(f"[AXIOM] 설정 저장 오류: {type(e).__name__}: {e}", file=sys.stderr)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed(f"저장하지 못했어요 ({type(e).__name__})"), ephemeral=True)


# ============================================================
# 티켓
# ============================================================
def tk_info(ch) -> tuple[int, int] | None:
    topic = getattr(ch, "topic", None)
    if not topic or not topic.startswith("tk:"):
        return None
    try:
        _, opener, claimer = topic.split(":")
        return int(opener), int(claimer)
    except ValueError:
        return None


def is_support(interaction: discord.Interaction) -> bool:
    if is_manager(interaction):
        return True
    role_id = gsec(interaction.guild_id, "ticket")["support"]
    return bool(role_id and any(r.id == role_id for r in getattr(interaction.user, "roles", [])))


TK_COOLDOWN: dict[int, float] = {}


class TicketOpenModal(discord.ui.Modal, title="티켓 열기"):
    reason = discord.ui.TextInput(label="문의 내용", style=discord.TextStyle.paragraph,
                                  placeholder="내용을 자세히 적어주세요", max_length=800)

    def __init__(self, ttype: str):
        super().__init__()
        self.ttype = ttype

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, user = interaction.guild, interaction.user
        cfg = gsec(guild.id, "ticket")

        mine = [c for c in guild.text_channels if (tk_info(c) or (0,))[0] == user.id]
        if len(mine) >= cfg["max_per_user"]:
            await interaction.followup.send(
                embed=error_embed(f"이미 열린 티켓이 있습니다: {mine[0].mention}"), ephemeral=True)
            return

        cfg["counter"] += 1
        save_gcfg()
        name = f"티켓-{cfg['counter']:04d}"
        support = guild.get_role(cfg["support"]) if cfg["support"] else None
        category = guild.get_channel(cfg["category"]) if cfg["category"] else None

        member_perm = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: member_perm,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, embed_links=True),
        }
        if support:
            overwrites[support] = member_perm

        try:
            ch = await guild.create_text_channel(
                name, category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites, topic=f"tk:{user.id}:0", reason=f"티켓 생성 ({user})")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed("채널을 만들 권한이 없습니다. 봇에 `채널 관리` 권한을 주세요."), ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(embed=error_embed(f"채널 생성 실패 ({e.status})"), ephemeral=True)
            return

        e = discord.Embed(title=f"{self.ttype}", description=self.reason.value, color=Theme.ACCENT)
        e.add_field(name="개설자", value=user.mention, inline=True)
        e.add_field(name="상태", value="대기 중", inline=True)
        e.set_footer(text="담당자가 곧 확인합니다. 닫으려면 아래 버튼을 누르세요.")
        content = user.mention + (f" {support.mention}" if support else "")
        await ch.send(content=content, embed=e, view=TicketControlView(),
                      allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await interaction.followup.send(
            embed=discord.Embed(title="티켓 생성 완료", description=f"{ch.mention} 에서 이어서 진행하세요.", color=Theme.OK),
            ephemeral=True)

        le = discord.Embed(title="티켓 생성", color=Theme.OK, timestamp=discord.utils.utcnow())
        le.add_field(name="티켓", value=ch.mention, inline=True)
        le.add_field(name="개설자", value=user.mention, inline=True)
        le.add_field(name="종류", value=self.ttype, inline=True)
        await send_log(guild, cfg["log"], le)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 티켓 생성 오류: {type(error).__name__}", file=sys.stderr)


class TicketPanelView(discord.ui.View):
    """티켓 열기 고정 패널 (재시작해도 유지)."""

    def __init__(self, types: list[str] | None = None):
        super().__init__(timeout=None)
        types = types or DEFAULTS["ticket"]["types"]
        sel = discord.ui.Select(
            placeholder="문의 종류를 선택하세요",
            custom_id="tk:open",
            options=[discord.SelectOption(label=t[:100], value=t[:100]) for t in types[:25]],
        )
        sel.callback = self.on_pick
        self.add_item(sel)

    async def on_pick(self, interaction: discord.Interaction):
        ttype = interaction.data["values"][0]
        now = time.time()
        if now - TK_COOLDOWN.get(interaction.user.id, 0) < 5:
            await interaction.response.send_message("잠시 후 다시 시도하세요.", ephemeral=True)
        else:
            TK_COOLDOWN[interaction.user.id] = now
            await interaction.response.send_modal(TicketOpenModal(ttype))
        try:
            await interaction.message.edit(view=self)  # 선택 표시 초기화
        except discord.HTTPException:
            pass


class CloseConfirm(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="닫기 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        info = tk_info(ch)
        await interaction.response.edit_message(
            embed=discord.Embed(title="티켓 종료", description="5초 후 채널이 삭제됩니다.", color=Theme.BUSY), view=None)
        cfg = gsec(interaction.guild_id, "ticket")
        opened = int(ch.created_at.timestamp())
        le = discord.Embed(title="티켓 종료", color=Theme.ERR, timestamp=discord.utils.utcnow())
        le.add_field(name="티켓", value=f"`{ch.name}`", inline=True)
        if info:
            le.add_field(name="개설자", value=f"<@{info[0]}>", inline=True)
            le.add_field(name="담당자", value=f"<@{info[1]}>" if info[1] else "없음", inline=True)
        le.add_field(name="종료자", value=interaction.user.mention, inline=True)
        le.add_field(name="개설 시각", value=f"<t:{opened}:f>", inline=True)
        await send_log(interaction.guild, cfg["log"], le)
        await asyncio.sleep(5)
        try:
            await ch.delete(reason=f"티켓 종료 ({interaction.user})")
        except discord.HTTPException:
            pass


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.danger, custom_id="tk:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = tk_info(interaction.channel)
        if info is None:
            await interaction.response.send_message("티켓 채널이 아닙니다.", ephemeral=True)
            return
        if interaction.user.id != info[0] and not is_support(interaction):
            await interaction.response.send_message("개설자 또는 담당자만 닫을 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(title="정말 닫을까요?", description="채널이 삭제되며 복구할 수 없습니다.", color=Theme.ERR),
            view=CloseConfirm(), ephemeral=True)

    @discord.ui.button(label="담당하기", style=discord.ButtonStyle.success, custom_id="tk:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        info = tk_info(ch)
        if info is None:
            await interaction.response.send_message("티켓 채널이 아닙니다.", ephemeral=True)
            return
        if not is_support(interaction):
            await interaction.response.send_message("담당자만 사용할 수 있습니다.", ephemeral=True)
            return
        if info[1]:
            await interaction.response.send_message(f"이미 <@{info[1]}> 님이 담당 중입니다.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await ch.edit(topic=f"tk:{info[0]}:{interaction.user.id}")
        except discord.HTTPException:
            pass
        await ch.send(embed=discord.Embed(description=f"{interaction.user.mention} 님이 담당합니다.", color=Theme.OK))


class TicketSettings(discord.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=300)
        self.add_item(CfgChannelSelect("ticket", "category", "티켓이 생길 카테고리", [discord.ChannelType.category], 0, gid))
        self.add_item(CfgChannelSelect("ticket", "log", "로그 채널", [discord.ChannelType.text], 1, gid))
        self.add_item(CfgRoleSelect("ticket", "support", "담당자 역할", 2, gid))

    def embed(self, gid: int, note: str = "") -> discord.Embed:
        c = gsec(gid, "ticket")
        e = discord.Embed(title="티켓 설정", description=note or "고르면 바로 저장돼요.", color=Theme.ACCENT)
        e.add_field(name="카테고리", value=mention("c", c["category"]), inline=True)
        e.add_field(name="로그 채널", value=mention("c", c["log"]), inline=True)
        e.add_field(name="담당자 역할", value=mention("r", c["support"]), inline=True)
        e.add_field(name="1인당 최대", value=f"{c['max_per_user']}개", inline=True)
        e.add_field(name="종류", value=" · ".join(c["types"]), inline=False)
        return e

    async def refresh(self, interaction: discord.Interaction, note: str = "저장했습니다."):
        await interaction.response.edit_message(embed=self.embed(interaction.guild_id, note), view=self)

    @discord.ui.button(label="상세 설정", style=discord.ButtonStyle.secondary, row=3)
    async def detail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketDetailModal(self))

    @discord.ui.button(label="패널 올리기", style=discord.ButtonStyle.primary, row=3)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = gsec(interaction.guild_id, "ticket")
        e = discord.Embed(title=f"{SHOP_NAME} 문의",
                          description="문의 종류를 고르면 1:1 채널이 열려요.", color=Theme.ACCENT)
        try:
            await interaction.channel.send(embed=e, view=TicketPanelView(c["types"]))
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("이 채널에 메시지를 보낼 수 없습니다."), ephemeral=True)
            return
        await interaction.response.send_message("패널을 올렸습니다.", ephemeral=True)


class TicketDetailModal(discord.ui.Modal, title="티켓 상세 설정"):
    maxn = discord.ui.TextInput(label="1인당 최대 티켓 수", default="1", max_length=2)
    types = discord.ui.TextInput(label="문의 종류 (쉼표로 구분)", style=discord.TextStyle.paragraph, max_length=400)

    def __init__(self, panel: TicketSettings):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        c = gsec(interaction.guild_id, "ticket")
        if self.maxn.value.strip().isdigit() and int(self.maxn.value) > 0:
            c["max_per_user"] = int(self.maxn.value)
        t = [x.strip()[:100] for x in self.types.value.split(",") if x.strip()][:25]
        if t:
            c["types"] = t
        save_gcfg()
        await self.panel.refresh(interaction)


# ============================================================
# 인증
# ============================================================
VERIFY_PENDING: dict[int, tuple[int, float]] = {}


async def grant_verify(interaction: discord.Interaction) -> None:
    guild, member = interaction.guild, interaction.user
    cfg = gsec(guild.id, "verify")
    role = guild.get_role(cfg["role"])
    try:
        await member.add_roles(role, reason="인증 완료")
        rm = guild.get_role(cfg["remove_role"]) if cfg["remove_role"] else None
        if rm and rm in member.roles:
            await member.remove_roles(rm, reason="인증 완료")
    except discord.Forbidden:
        await interaction.followup.send(
            embed=error_embed("역할을 줄 권한이 없습니다. 봇 역할을 인증 역할보다 위로 올려주세요."), ephemeral=True)
        return
    await interaction.followup.send(
        embed=discord.Embed(title="인증 완료", description=f"{role.mention} 역할이 지급되었습니다.", color=Theme.OK),
        ephemeral=True)
    le = discord.Embed(title="인증 완료", color=Theme.OK, timestamp=discord.utils.utcnow())
    le.add_field(name="유저", value=member.mention, inline=True)
    le.add_field(name="계정 생성", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    await send_log(guild, cfg["log"], le)


class CaptchaModal(discord.ui.Modal, title="인증"):
    answer = discord.ui.TextInput(label="정답", max_length=6)

    def __init__(self, question: str):
        super().__init__()
        self.answer.label = question

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ans, exp = VERIFY_PENDING.pop(interaction.user.id, (None, 0))
        if ans is None or time.time() > exp:
            await interaction.followup.send(embed=error_embed("시간이 지났습니다. 다시 시도하세요."), ephemeral=True)
            return
        if self.answer.value.strip() != str(ans):
            await interaction.followup.send(embed=error_embed("오답입니다. 버튼을 다시 눌러주세요."), ephemeral=True)
            return
        await grant_verify(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 인증 오류: {type(error).__name__}", file=sys.stderr)


class VerifyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="인증하기", style=discord.ButtonStyle.success, custom_id="vf:btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = interaction.guild, interaction.user
        cfg = gsec(guild.id, "verify")
        role = guild.get_role(cfg["role"]) if cfg["role"] else None
        if role is None:
            await interaction.response.send_message("인증 역할이 설정되지 않았습니다. 관리자에게 문의하세요.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("이미 인증된 계정입니다.", ephemeral=True)
            return
        age = (discord.utils.utcnow() - member.created_at).days
        if age < cfg["min_age"]:
            await interaction.response.send_message(
                embed=error_embed(f"계정 생성 후 {cfg['min_age']}일이 지나야 인증할 수 있습니다. (현재 {age}일)"), ephemeral=True)
            return
        if cfg["mode"] == "captcha":
            a, b = random.randint(2, 19), random.randint(2, 19)
            op = random.choice("+-*")
            ans = {"+": a + b, "-": a - b, "*": a * b}[op]
            VERIFY_PENDING[member.id] = (ans, time.time() + 120)
            await interaction.response.send_modal(CaptchaModal(f"{a} {op} {b} = ?"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await grant_verify(interaction)


class VerifyAgeModal(discord.ui.Modal, title="최소 계정 나이"):
    days = discord.ui.TextInput(label="계정 생성 후 최소 일수 (0 = 제한 없음)", default="0", max_length=4)

    def __init__(self, panel: "VerifySettings"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        v = self.days.value.strip()
        gsec(interaction.guild_id, "verify")["min_age"] = int(v) if v.isdigit() else 0
        save_gcfg()
        await self.panel.refresh(interaction)


class VerifySettings(discord.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=300)
        self.add_item(CfgRoleSelect("verify", "role", "인증 시 지급할 역할", 0, gid))
        self.add_item(CfgRoleSelect("verify", "remove_role", "인증 시 제거할 역할 (선택)", 1, gid))
        self.add_item(CfgChannelSelect("verify", "log", "로그 채널", [discord.ChannelType.text], 2, gid))

    def embed(self, gid: int, note: str = "") -> discord.Embed:
        c = gsec(gid, "verify")
        e = discord.Embed(title="인증 설정", description=note or "고르면 바로 저장돼요.", color=Theme.ACCENT)
        e.add_field(name="지급 역할", value=mention("r", c["role"]), inline=True)
        e.add_field(name="제거 역할", value=mention("r", c["remove_role"]), inline=True)
        e.add_field(name="로그 채널", value=mention("c", c["log"]), inline=True)
        e.add_field(name="방식", value="버튼" if c["mode"] == "button" else "퀴즈(계산)", inline=True)
        e.add_field(name="최소 계정 나이", value=f"{c['min_age']}일" if c["min_age"] else "제한 없음", inline=True)
        return e

    async def refresh(self, interaction: discord.Interaction, note: str = "저장했습니다."):
        await interaction.response.edit_message(embed=self.embed(interaction.guild_id, note), view=self)

    @discord.ui.button(label="방식 전환", style=discord.ButtonStyle.secondary, row=3)
    async def mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = gsec(interaction.guild_id, "verify")
        c["mode"] = "captcha" if c["mode"] == "button" else "button"
        save_gcfg()
        await self.refresh(interaction)

    @discord.ui.button(label="계정 나이", style=discord.ButtonStyle.secondary, row=3)
    async def age(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyAgeModal(self))

    @discord.ui.button(label="제거 역할 해제", style=discord.ButtonStyle.secondary, row=3)
    async def clear_rm(self, interaction: discord.Interaction, button: discord.ui.Button):
        gsec(interaction.guild_id, "verify")["remove_role"] = 0
        save_gcfg()
        await self.refresh(interaction)

    @discord.ui.button(label="패널 올리기", style=discord.ButtonStyle.primary, row=3)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = discord.Embed(title=f"{SHOP_NAME} 인증",
                          description="버튼을 누르면 인증이 끝나고 모든 채널을 볼 수 있어요.", color=Theme.ACCENT)
        try:
            await interaction.channel.send(embed=e, view=VerifyPanelView())
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("이 채널에 메시지를 보낼 수 없습니다."), ephemeral=True)
            return
        await interaction.response.send_message("패널을 올렸습니다.", ephemeral=True)


# ============================================================
# 고양이 ID 검색 (채널에 이름만 치면 답장)
# ============================================================
RAR_ICON = {0: "⚪", 1: "🟤", 2: "🟢", 3: "🔵", 4: "🟣", 5: "⭐"}
_CAT_LOCK = threading.Lock()
_CAT_SF = None
_CAT_INDEX: list | None = None
_CAT_ERR = ""
_cat_last: dict[int, float] = {}


def _form_names(cat, sf) -> list[str]:
    """form별 이름 목록. bcsfe 버전마다 속성이 달라서 여러 방식으로 시도."""
    for getter in ("get_names_cls", "get_names"):
        fn = getattr(cat, getter, None)
        if not callable(fn):
            continue
        try:
            r = fn(sf)
        except Exception:
            continue
        for attr in ("names", "name"):
            if r is not None and not isinstance(r, (list, tuple)) and hasattr(r, attr):
                r = getattr(r, attr)
                break
        if isinstance(r, (list, tuple)):
            return [str(x).strip() for x in r]
    r = getattr(cat, "names", None)
    if isinstance(r, (list, tuple)):
        return [str(x).strip() for x in r]
    return []


def _build_cat_index():
    global _CAT_SF, _CAT_INDEX, _CAT_ERR
    if _CAT_INDEX is not None:
        return
    sf, _country, err = cached_base()
    if sf is None:
        _CAT_ERR = err
        return
    rarity: dict[int, int] = {}
    for r in RARITIES:
        try:
            for c in sf.cats.get_cats_rarity(sf, r):
                rarity[c.id] = r
        except Exception:
            pass
    idx = []
    first = None
    for cat in sf.cats.get_all_cats():
        first = first or cat
        names = _form_names(cat, sf)
        if any(names):
            idx.append((cat.id, rarity.get(cat.id, 0), names))
    if not idx and first is not None:
        print("[AXIOM] 고양이 이름 추출 실패. Cat 속성:", [m for m in dir(first) if not m.startswith("_")], file=sys.stderr)
    _CAT_INDEX, _CAT_ERR = idx, ""
    _CAT_SF = None if idx else sf          # 이름 추출 실패 때만 대체 검색용으로 보관
    if not FILL_LOCK.locked():
        _BASE_CACHE["sf"] = None            # 색인만 남기고 템플릿 메모리 반납
        gc.collect()


def search_cats(query: str):
    """(결과 리스트 | None, 에러)  결과: (id, rarity, [form names])"""
    with _CAT_LOCK:
        _build_cat_index()
        if _CAT_INDEX is None:
            return None, _CAT_ERR or "데이터를 불러오지 못했습니다."
        q = re.sub(r"\s+", "", query).lower()
        if q.isdigit():
            hits = [h for h in _CAT_INDEX if h[0] == int(q)]
        else:
            hits = [h for h in _CAT_INDEX if any(q in re.sub(r"\s+", "", n).lower() for n in h[2] if n)]
        if not hits and not _CAT_INDEX and _CAT_SF is not None and not q.isdigit():
            try:  # 이름 추출이 안 되면 bcsfe 자체 검색으로 ID라도 제공
                hits = [(c.id, 0, []) for c in _CAT_SF.cats.get_cats_name(_CAT_SF, query)]
            except Exception:
                hits = []
        return hits, ""


def cat_embed(user: discord.abc.User, query: str, hits: list) -> discord.Embed:
    q = re.sub(r"\s+", "", query).lower()
    e = discord.Embed(title=f"🔍 '{query}' 검색 결과", color=Theme.ACCENT)
    e.set_author(name=f"{user.display_name}님의 검색", icon_url=user.display_avatar.url)
    if not hits:
        e.description = "찾지 못했어요. 이름을 다시 확인해 주세요."
    else:
        e.description = f"총 {len(hits)}건을 찾았어요 · 굵은 글씨가 검색한 이름이에요"
        for cid, rar, names in hits[:8]:
            shown = [n for n in names if n]
            title = shown[0] if shown else "이름 정보 없음"
            lines = []
            for i, n in enumerate(names, 1):
                if not n:
                    continue
                hit = q and q in re.sub(r"\s+", "", n).lower()
                lines.append(f"`{i}단` {'**' + n + '**' if hit else n}")
            e.add_field(name=f"{RAR_ICON.get(rar, '⚪')} ID {cid} · {title}"[:256],
                        value="\n".join(lines) or "​", inline=False)
        if len(hits) > 8:
            e.description += f"\n상위 8건만 표시했어요. 이름을 더 정확히 입력해 보세요."
    e.set_footer(text=f"{SHOP_NAME} · 번호(ID)를 복사해서 쓰세요")
    return e


async def handle_cat(message: discord.Message):
    query = (message.content or "").strip()
    if not query or len(query) > 30 or query.startswith(("/", "!")):
        return
    uid = message.author.id
    if uid in SEC["blocked"]:
        return
    now = time.monotonic()
    if now - _cat_last.get(uid, -1e9) < 3:
        return
    _cat_last[uid] = now
    loop = asyncio.get_running_loop()
    hits, err = await loop.run_in_executor(None, search_cats, query)
    if hits is None:
        await message.reply(embed=error_embed(err), delete_after=10, mention_author=False)
        return
    await message.reply(embed=cat_embed(message.author, query, hits), mention_author=False)


# ============================================================
# 무료젠 (채널에 아무 채팅 → 확률 지급, 재고제)
# ============================================================
GEN_TIER_KEYS = ["basic", "mid", "high"]
GEN_LOCK = asyncio.Lock()
FILL_LOCK = asyncio.Lock()
FILL_WORKERS = 1      # 동시 생성 수 (메모리 256MB 기준). 여유 있으면 올리세요
PREWARM = False       # True면 봇 시작 때 고양이 색인을 미리 만듦 (메모리 여유 있을 때만)


def _pick_stock_tier(weights: list[float]) -> str | None:
    want = random.choices(GEN_TIER_KEYS, weights=weights, k=1)[0]
    i = GEN_TIER_KEYS.index(want)
    for k in [GEN_TIER_KEYS[i]] + GEN_TIER_KEYS[:i][::-1] + GEN_TIER_KEYS[i + 1:]:
        if STOCK.get(k):
            return k
    return None


def stock_total() -> int:
    return sum(len(STOCK.get(k, [])) for k in GEN_TIER_KEYS)


async def handle_gen(message: discord.Message):
    uid = message.author.id
    if uid in SEC["blocked"]:
        return
    cfg = gsec(message.guild.id, "free")

    async def say(embed: discord.Embed, wait: int | None = None):
        try:
            await message.reply(embed=embed, delete_after=wait, mention_author=False)
        except discord.HTTPException:
            pass

    async with GEN_LOCK:
        now = time.time()
        if uid not in ADMIN_IDS:
            cd = cfg["gen_cooldown_hours"]
            nxt = FREE["gen_last"].get(str(uid), 0) + cd * 3600
            if cd > 0 and nxt > now:
                await say(error_embed(f"이미 받으셨어요. <t:{int(nxt)}:R> 다시 받을 수 있어요."), 8)
                return
            if review_blocked(message.guild.id, uid):
                await say(error_embed(REVIEW_MSG), 10)
                return
        tier = _pick_stock_tier(cfg["weights"])
        if tier is None:
            await say(error_embed("지금은 재고가 없어요. 관리자에게 문의해주세요."), 10)
            return
        codes = STOCK[tier].pop(0)
        save_stock()
        card = code_embed(codes, "무료젠 계정")
        card.add_field(name="등급", value=TIERS[tier][0], inline=True)
        if not await send_dm(message.author, embeds=[card]):
            STOCK[tier].insert(0, codes)
            save_stock()
            await say(error_embed("DM을 보낼 수 없어요. 서버 설정에서 DM을 허용한 뒤 다시 시도해주세요."), 10)
            return
        FREE["gen_last"][str(uid)] = now
        FREE["gen_total"] += 1
        mark_need_review(uid)
        left = stock_total()

    e = discord.Embed(
        title="✅ 무료젠 지급 완료",
        description=f"DM을 확인해주세요. 무료계정을 보냈어요!\n남은 재고: {left}",
        color=discord.Color.from_rgb(46, 196, 182),
    )
    await say(e)
    await use_log(message.guild, message.author, "무료젠 이용")


# ============================================================
# 이용 후기 (작성 필수 패널)
# ============================================================
_review_last: dict[int, float] = {}


class ReviewModal(discord.ui.Modal, title="이용 후기"):
    stars = discord.ui.TextInput(label="별점 (1~5)", default="5", min_length=1, max_length=1)
    content = discord.ui.TextInput(label="후기 내용", style=discord.TextStyle.paragraph,
                                   placeholder="이용하신 소감을 남겨주세요", min_length=5, max_length=600)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if time.time() - _review_last.get(uid, 0) < 60:
            await interaction.response.send_message("잠시 후 다시 작성해주세요.", ephemeral=True)
            return
        n = int(self.stars.value) if self.stars.value.isdigit() else 5
        n = max(1, min(5, n))
        cfg = gsec(interaction.guild_id, "free")
        target = interaction.guild.get_channel(cfg["review_channel"]) if cfg["review_channel"] else None
        target = target or interaction.channel
        e = discord.Embed(title="⭐" * n, description=self.content.value, color=Theme.ACCENT,
                          timestamp=discord.utils.utcnow())
        e.set_author(name=f"{interaction.user.display_name}님의 후기", icon_url=interaction.user.display_avatar.url)
        e.set_footer(text=f"{SHOP_NAME} 이용 후기")
        try:
            await target.send(embed=e)
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("후기 채널에 보낼 수 없습니다. 관리자에게 문의해주세요."), ephemeral=True)
            return
        _review_last[uid] = time.time()
        if str(uid) in FREE["need_review"]:
            FREE["need_review"].remove(str(uid))
            save_free()
        await interaction.response.send_message(
            embed=discord.Embed(title="후기 감사합니다", description="이제 다시 이용하실 수 있어요.", color=Theme.OK),
            ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 후기 오류: {type(error).__name__}", file=sys.stderr)


class ReviewPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="후기 작성하기", style=discord.ButtonStyle.success, custom_id="rv:write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReviewModal())


def review_embed() -> discord.Embed:
    e = discord.Embed(
        title="이용 후기",
        description=(
            "무료통조림이나 무료젠을 쓰셨다면 후기를 남겨주세요.\n"
            "후기를 써야 다시 이용할 수 있어요."
        ),
        color=Theme.MAIN,
    )
    e.set_author(name=SHOP_NAME)
    return e


# ============================================================
# 세이브 관리 (백업 / 복원 / 파일 관리 / 복사하기)
# ============================================================
VAULT_DIR = BASE_DIR / "vault"
VAULT_KEY = BASE_DIR / "vault.key"
MAX_BACKUPS = 10


def _fernet():
    if Fernet is None:
        raise RuntimeError("cryptography 모듈이 필요합니다: pip install cryptography")
    if not VAULT_KEY.exists():
        VAULT_KEY.write_bytes(Fernet.generate_key())
    return Fernet(VAULT_KEY.read_bytes())


def vault_dir(uid: int) -> Path:
    d = VAULT_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def vault_list(uid: int) -> list[Path]:
    d = VAULT_DIR / str(uid)
    return sorted(d.glob("*.vault"), reverse=True) if d.exists() else []


def vault_meta(path: Path) -> tuple[str, str, str]:
    """(시각, 국가, 이름)"""
    try:
        ts, country, label = path.stem.split("__", 2)
        when = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%m-%d %H:%M")
        return when, country, label
    except ValueError:
        return "?", "kr", path.stem


def vault_put(uid: int, sf, country: str, label: str):
    try:
        f = _fernet()
        tmp = vault_dir(uid) / "_tmp.sav"
        sf.to_data().to_file(core.Path(str(tmp)))
        raw = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        clean = re.sub(r"[^0-9A-Za-z가-힣\-]", "", label)[:15] or "백업"
        path = vault_dir(uid) / f"{datetime.now():%Y%m%d_%H%M%S}__{country}__{clean}.vault"
        path.write_bytes(f.encrypt(raw))
        return path.name, ""
    except Exception as e:
        print(f"[AXIOM] 금고 저장 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return None, f"{type(e).__name__}: {e}"


def load_save_bytes(raw: bytes, country: str):
    init_core()
    cc = make_country_code(country)
    gv = core.GameVersion(GAME_VERSION)
    dt = core.Data(raw)
    last = None
    for attempt in (
        lambda: core.SaveFile(dt=dt, cc=cc, load=True, gv=gv),
        lambda: core.SaveFile(dt, cc, True, gv),
    ):
        try:
            return attempt()
        except Exception as e:  # 시그니처가 다르면 다음 방식 시도
            last = e
    raise RuntimeError(f"세이브 로드 실패: {type(last).__name__}: {last}")


def vault_restore(path: Path):
    try:
        raw = _fernet().decrypt(path.read_bytes())
        _, country, _ = vault_meta(path)
        sf = load_save_bytes(raw, country)
        codes, err, _ = apply_and_upload(sf, {}, True, [])
        return codes, err
    except InvalidToken:
        return None, "백업 파일을 복호화할 수 없습니다."
    except Exception as e:
        print(f"[AXIOM] 복원 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return None, f"{type(e).__name__}: {e}"


def vault_options(paths: list[Path]) -> list[discord.SelectOption]:
    out = []
    for p in paths[:25]:
        when, country, label = vault_meta(p)
        out.append(discord.SelectOption(label=f"{label} · {when}"[:100], value=p.name,
                                        description=f"{COUNTRIES.get(country, country)} · {p.stat().st_size // 1024}KB"))
    return out


def safe_vault_path(uid: int, name: str) -> Path | None:
    if name != Path(name).name or not name.endswith(".vault"):
        return None
    p = VAULT_DIR / str(uid) / name
    return p if p.exists() else None


def save_embed() -> discord.Embed:
    e = discord.Embed(
        title="💾 냥코대전쟁 세이브 관리",
        description=(
            "계정을 잃어버리거나 작업이 잘못됐을 때를 대비해 세이브를 미리 백업해 두세요.\n\n"
            "💾 **백업하기** · 이어하기 코드와 인증번호로 세이브를 암호화해서 보관해요\n"
            "♻️ **복원하기** · 백업해 둔 세이브를 새 이어하기 코드로 되돌려요\n"
            "📁 **파일 관리** · 내 백업 목록을 보고 삭제해요\n"
            "📋 **복사하기** · 관리자 전용 (만일의 대비)\n\n"
            "⚠️ **미리 백업해 둔 계정만 복원할 수 있어요. 이미 잃어버린 계정은 복원할 수 없어요.**"
        ),
        color=Theme.ACCENT,
    )
    e.set_footer(text=f"{SHOP_NAME} 세이브봇 · 입력한 코드는 저장하지 않아요")
    return e


class BackupModal(discord.ui.Modal, title="백업하기"):
    transfer_code = discord.ui.TextInput(label="이어하기 코드", min_length=4, max_length=30)
    confirmation_code = discord.ui.TextInput(label="비밀번호", min_length=4, max_length=20)
    label = discord.ui.TextInput(label="백업 이름 (선택)", required=False, max_length=15, placeholder="예: 메인계정")

    def __init__(self, country: str):
        super().__init__()
        self.country = country

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if uid not in ADMIN_IDS and len(vault_list(uid)) >= MAX_BACKUPS:
            await interaction.response.send_message(
                embed=error_embed(f"백업은 최대 {MAX_BACKUPS}개까지입니다. 파일 관리에서 정리해주세요."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()
        sf, err = await loop.run_in_executor(
            None, download_save, self.transfer_code.value.strip(), self.confirmation_code.value.strip(), self.country)
        if sf is None:
            await interaction.followup.send(embed=error_embed(err), ephemeral=True)
            return
        name, err = await loop.run_in_executor(None, vault_put, uid, sf, self.country, self.label.value or "백업")
        if name is None:
            await interaction.followup.send(embed=error_embed(err), ephemeral=True)
            return
        await interaction.followup.send(
            embed=discord.Embed(title="💾 백업 완료", description="세이브를 암호화해서 보관했어요.\n복원하기에서 되돌릴 수 있어요.", color=Theme.OK),
            ephemeral=True)
        await use_log(interaction.guild, interaction.user, "세이브 백업")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 백업 오류: {type(error).__name__}", file=sys.stderr)


async def do_restore(interaction: discord.Interaction, owner_id: int, filename: str, log_text: str):
    """owner_id의 백업을 새 이어하기 코드로 복원해서 interaction 유저에게 DM."""
    path = safe_vault_path(owner_id, filename)
    if path is None:
        await interaction.response.edit_message(embed=error_embed("백업 파일을 찾을 수 없습니다."), view=None)
        return
    await interaction.response.edit_message(embed=busy_embed("복원하는 중", 0, 1), view=None)
    loop = asyncio.get_running_loop()
    codes, err = await loop.run_in_executor(None, vault_restore, path)
    if codes is None:
        await interaction.edit_original_response(embed=error_embed(err))
        return
    card = code_embed(codes, "♻️ 복원된 이어하기 정보")
    if await send_dm(interaction.user, embeds=[card]):
        await interaction.edit_original_response(
            embed=discord.Embed(title="♻️ 복원 완료", description="새 이어하기 코드와 비밀번호를 DM으로 보냈어요.", color=Theme.OK))
    else:
        card.description = "DM을 보낼 수 없어 여기에 표시합니다. 지금 복사해 두세요."
        await interaction.edit_original_response(embed=card)
    await use_log(interaction.guild, interaction.user, log_text)


class RestoreSelect(discord.ui.Select):
    def __init__(self, owner_id: int, paths: list[Path], log_text: str):
        super().__init__(placeholder="복원할 백업을 선택하세요", options=vault_options(paths))
        self.owner_id, self.log_text = owner_id, log_text

    async def callback(self, interaction: discord.Interaction):
        await do_restore(interaction, self.owner_id, self.values[0], self.log_text)


class DeleteSelect(discord.ui.Select):
    def __init__(self, uid: int, paths: list[Path]):
        super().__init__(placeholder="삭제할 백업을 선택하세요", options=vault_options(paths))
        self.uid = uid

    async def callback(self, interaction: discord.Interaction):
        p = safe_vault_path(self.uid, self.values[0])
        if p:
            p.unlink(missing_ok=True)
        await interaction.response.edit_message(embed=files_embed(self.uid), view=files_view(self.uid))


def files_embed(uid: int) -> discord.Embed:
    paths = vault_list(uid)
    e = discord.Embed(title="📁 내 백업 파일", color=Theme.ACCENT)
    if not paths:
        e.description = "저장된 백업이 없어요."
    else:
        rows = []
        for p in paths:
            when, country, label = vault_meta(p)
            rows.append(f"**{label}** · {COUNTRIES.get(country, country)} · {when}")
        e.description = "\n".join(rows)
    e.set_footer(text=f"{len(paths)}/{MAX_BACKUPS}")
    return e


def files_view(uid: int) -> discord.ui.View | None:
    paths = vault_list(uid)
    if not paths:
        return None
    v = discord.ui.View(timeout=120)
    v.add_item(DeleteSelect(uid, paths))
    return v


class CopyUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="복사할 유저를 선택하세요", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        paths = vault_list(target.id)
        if not paths:
            await interaction.response.edit_message(
                embed=error_embed(f"{target.mention} 님의 백업이 없습니다."), view=self.view)
            return
        v = discord.ui.View(timeout=120)
        v.add_item(RestoreSelect(target.id, paths, f"{target.display_name}님의 백업 복사"))
        await interaction.response.edit_message(
            embed=discord.Embed(title="📋 복사할 백업 선택", description=f"{target.mention} 님의 백업 {len(paths)}개", color=Theme.ACCENT),
            view=v)


class SaveView(discord.ui.View):
    """재시작해도 유지되는 세이브 관리 패널."""

    def __init__(self, country: str):
        super().__init__(timeout=None)
        self.country = country
        spec = [
            ("백업하기", discord.ButtonStyle.primary, "💾", "backup", self.on_backup),
            ("복원하기", discord.ButtonStyle.success, "♻️", "restore", self.on_restore),
            ("파일 관리", discord.ButtonStyle.secondary, "📁", "files", self.on_files),
            ("복사하기 (관리자)", discord.ButtonStyle.secondary, "📋", "copy", self.on_copy),
        ]
        for label, style, emoji, key, cb in spec:
            b = discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"sv:{key}:{country}")
            b.callback = cb
            self.add_item(b)

    async def on_backup(self, interaction: discord.Interaction):
        if await guard(interaction, "save"):
            await interaction.response.send_modal(BackupModal(self.country))

    async def on_restore(self, interaction: discord.Interaction):
        if not await guard(interaction, "save"):
            return
        paths = vault_list(interaction.user.id)
        if not paths:
            await interaction.response.send_message(embed=error_embed("저장된 백업이 없어요. 먼저 백업하기를 눌러주세요."), ephemeral=True)
            return
        v = discord.ui.View(timeout=120)
        v.add_item(RestoreSelect(interaction.user.id, paths, "세이브 복원"))
        await interaction.response.send_message(
            embed=discord.Embed(title="♻️ 복원할 백업 선택", description="선택하면 새 이어하기 코드를 DM으로 보내드려요.", color=Theme.ACCENT),
            view=v, ephemeral=True)

    async def on_files(self, interaction: discord.Interaction):
        if not await guard(interaction, "save"):
            return
        await interaction.response.send_message(
            embed=files_embed(interaction.user.id), view=files_view(interaction.user.id), ephemeral=True)

    async def on_copy(self, interaction: discord.Interaction):
        if interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        v = discord.ui.View(timeout=120)
        v.add_item(CopyUserSelect())
        await interaction.response.send_message(
            embed=discord.Embed(title="📋 복사하기", description="백업을 복사할 유저를 선택하세요.", color=Theme.ACCENT),
            view=v, ephemeral=True)


# ============================================================
# 무료젠 / 검색 / 후기 설정 패널
# ============================================================
def _pct(weights: list[float]) -> str:
    total = sum(weights) or 1
    return " · ".join(f"{TIERS[k][0]} {w / total * 100:g}%" for k, w in zip(GEN_TIER_KEYS, weights))


class FreeValuesModal(discord.ui.Modal, title="쿨타임 · 확률 설정"):
    gen_cd = discord.ui.TextInput(label="무료젠 대기시간 (시간, 0=제한 없음)", max_length=5)
    weights = discord.ui.TextInput(label="확률 기본,중급,고급 (예: 80,19,1)", max_length=20)
    can_cd = discord.ui.TextInput(label="무료통조림 대기시간 (시간, 0=제한 없음)", max_length=6)

    def __init__(self, panel: "FreeSettings", gid: int):
        super().__init__()
        self.panel = panel
        c = gsec(gid, "free")
        self.gen_cd.default = str(c["gen_cooldown_hours"])
        self.weights.default = ",".join(f"{w:g}" for w in c["weights"])
        self.can_cd.default = str(FREE["cooldown_hours"])

    async def on_submit(self, interaction: discord.Interaction):
        c = gsec(interaction.guild_id, "free")
        note = "저장했습니다."
        if self.gen_cd.value.strip().isdigit():
            c["gen_cooldown_hours"] = int(self.gen_cd.value)
        if self.can_cd.value.strip().isdigit():
            FREE["cooldown_hours"] = int(self.can_cd.value)
            save_free()
        try:
            w = [float(x) for x in self.weights.value.replace(" ", "").split(",")]
            if len(w) != 3 or min(w) < 0 or sum(w) <= 0:
                raise ValueError
            c["weights"] = w
        except ValueError:
            note = "확률 형식이 잘못되어 확률은 바뀌지 않았습니다. (예: 80,19,1)"
        save_gcfg()
        await self.panel.refresh(interaction, note)


class StockModal(discord.ui.Modal, title="재고 채우기"):
    basic = discord.ui.TextInput(label="기본 계정 개수 (최대 100)", default="100", max_length=3)
    mid = discord.ui.TextInput(label="중급 계정 개수 (최대 100)", default="100", max_length=3)
    high = discord.ui.TextInput(label="고급 계정 개수 (최대 100)", default="100", max_length=3)

    def __init__(self, panel: "FreeSettings"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        def num(t):
            return min(100, int(t.value)) if t.value.strip().isdigit() else 0

        plan = [("basic", num(self.basic)), ("mid", num(self.mid)), ("high", num(self.high))]
        total = sum(n for _, n in plan)
        if total <= 0:
            await self.panel.refresh(interaction, "개수를 입력해주세요.")
            return
        if FILL_LOCK.locked():
            await self.panel.refresh(interaction, "이미 재고를 채우는 중입니다.")
            return
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        done, fail = 0, ""
        last_edit = [0.0]
        pool = ThreadPoolExecutor(max_workers=min(total, FILL_WORKERS))

        async def job(tier: str):
            nonlocal done, fail
            res, err = await loop.run_in_executor(pool, generate_one, tier)
            if res is None:
                fail = fail or err
                return
            STOCK[tier].append(list(res["codes"]))
            save_stock()
            done += 1
            now = time.monotonic()
            if now - last_edit[0] > 1.5 or done == total:
                last_edit[0] = now
                try:
                    await interaction.edit_original_response(embed=busy_embed("재고를 채우는 중", done, total))
                except discord.HTTPException:
                    pass

        async with FILL_LOCK:
            await interaction.edit_original_response(embed=busy_embed("재고를 채우는 중", 0, total))
            try:
                await asyncio.gather(*(job(t) for t, n in plan for _ in range(n)))
            finally:
                pool.shutdown(wait=False)
                _BASE_CACHE["sf"] = None   # 템플릿 메모리 반납
                gc.collect()
        note = f"재고 {done}개를 채웠습니다." + (f" 중단: {fail}" if fail else "")
        try:
            await interaction.edit_original_response(embed=self.panel.embed(interaction.guild_id, note), view=self.panel)
        except discord.HTTPException:
            pass


class FreeSettings(discord.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=600)
        t = [discord.ChannelType.text]
        self.add_item(CfgChannelSelect("free", "gen_channel", "무료젠 채널 (아무 채팅 → 지급)", t, 0, gid))
        self.add_item(CfgChannelSelect("free", "cat_channel", "고양이 ID 검색 채널", t, 1, gid))
        self.add_item(CfgChannelSelect("free", "log_channel", "이용 로그 채널", t, 2, gid))
        self.add_item(CfgChannelSelect("free", "review_channel", "이용 후기 채널", t, 3, gid))

    def embed(self, gid: int, note: str = "") -> discord.Embed:
        c = gsec(gid, "free")
        e = discord.Embed(title="채널 · 쿨타임 설정", description=note or "채널을 고르면 바로 저장돼요.", color=Theme.ACCENT)
        e.add_field(name="무료젠 채널", value=mention("c", c["gen_channel"]), inline=True)
        e.add_field(name="검색 채널", value=mention("c", c["cat_channel"]), inline=True)
        e.add_field(name="이용 로그", value=mention("c", c["log_channel"]), inline=True)
        e.add_field(name="후기 채널", value=mention("c", c["review_channel"]), inline=True)
        e.add_field(name="후기 작성 필수", value="켜짐" if c["review_required"] else "꺼짐", inline=True)
        e.add_field(name="무료젠 대기시간", value=f"{c['gen_cooldown_hours']}시간" if c["gen_cooldown_hours"] else "제한 없음", inline=True)
        e.add_field(name="무료통조림 대기시간", value=f"{FREE['cooldown_hours']}시간" if FREE["cooldown_hours"] else "제한 없음", inline=True)
        e.add_field(name="확률", value=_pct(c["weights"]), inline=False)
        e.add_field(name="재고", value=" · ".join(f"{TIERS[k][0]} {len(STOCK.get(k, []))}" for k in GEN_TIER_KEYS), inline=False)
        return e

    async def refresh(self, interaction: discord.Interaction, note: str = "저장했습니다."):
        await interaction.response.edit_message(embed=self.embed(interaction.guild_id, note), view=self)

    @discord.ui.button(label="쿨타임 · 확률", style=discord.ButtonStyle.secondary, row=4)
    async def values(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FreeValuesModal(self, interaction.guild_id))

    @discord.ui.button(label="재고 채우기", style=discord.ButtonStyle.primary, row=4)
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StockModal(self))

    @discord.ui.button(label="후기 필수 전환", style=discord.ButtonStyle.secondary, row=4)
    async def review_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = gsec(interaction.guild_id, "free")
        c["review_required"] = not c["review_required"]
        save_gcfg()
        await self.refresh(interaction)


class AxiomBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # 고양이 검색용 (개발자 포털에서 Message Content Intent 켜야 함)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sessions: dict[int, Session] = {}

    async def _prewarm(self):
        """시작할 때 템플릿과 고양이 이름 색인을 미리 만들어 둠 (첫 검색/재고 채우기가 빨라짐)."""
        def work():
            with _CAT_LOCK:
                _build_cat_index()
        try:
            await asyncio.get_running_loop().run_in_executor(None, work)
            print("[AXIOM] 고양이 색인 준비 완료" if _CAT_INDEX else f"[AXIOM] 고양이 색인 준비 실패: {_CAT_ERR or '이름 데이터 없음'}")
        except Exception as e:
            print(f"[AXIOM] 사전 준비 오류: {type(e).__name__}: {e}", file=sys.stderr)

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        cfg = gsec(message.guild.id, "free")
        try:
            if cfg["gen_channel"] and message.channel.id == cfg["gen_channel"]:
                await handle_gen(message)
            elif cfg["cat_channel"] and message.channel.id == cfg["cat_channel"]:
                await handle_cat(message)
        except Exception as e:
            print(f"[AXIOM] 메시지 처리 오류: {type(e).__name__}: {e}", file=sys.stderr)

    async def setup_hook(self):
        if PREWARM:
            asyncio.create_task(self._prewarm())
        for c in COUNTRIES:
            self.add_view(EditorHub(c))
            self.add_view(QuickView(c))
            self.add_view(SaveView(c))
        self.add_view(RandomPanelView())
        self.add_view(ReviewPanelView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        self.add_view(VerifyPanelView())
        synced = await self.tree.sync()
        print(f"[AXIOM] 슬래시 커맨드 {len(synced)}개 동기화")


bot = AxiomBot()






class CountModal(discord.ui.Modal, title="랜덤 계정"):
    count = discord.ui.TextInput(label="생성 개수", default="1", min_length=1, max_length=4)

    def __init__(self, tier: str):
        super().__init__()
        self.tier = tier

    async def on_submit(self, interaction: discord.Interaction):
        v = self.count.value.strip()
        n = int(v) if v.isdigit() and int(v) > 0 else 1
        await do_random(interaction, self.tier, n)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 랜덤계정 오류: {type(error).__name__}", file=sys.stderr)


class RandomPanelView(discord.ui.View):
    """재시작해도 살아있는 랜덤계정 고정 패널."""

    def __init__(self):
        super().__init__(timeout=None)
        sel = discord.ui.Select(
            placeholder="등급을 선택하세요",
            custom_id="rand:panel",
            options=[discord.SelectOption(label=v[0], value=k) for k, v in TIERS.items()],
        )
        sel.callback = self.on_pick
        self.add_item(sel)

    async def on_pick(self, interaction: discord.Interaction):
        tier = interaction.data["values"][0]
        if await guard(interaction, "create"):
            await interaction.response.send_modal(CountModal(tier))
        try:
            await interaction.edit_original_response(view=RandomPanelView())
        except discord.HTTPException:
            pass




async def do_random(interaction: discord.Interaction, tier: str, 개수: int):
    await interaction.response.send_message(embed=busy_embed(f"계정 {개수}개 생성 중", 0, 개수), ephemeral=True)

    loop = asyncio.get_running_loop()
    ok_results: list[dict] = []
    fail_msg = ""
    for i in range(개수):
        res, err = await loop.run_in_executor(None, generate_one, tier)
        if res is None:
            fail_msg = f"#{i + 1} {err}"
            break
        ok_results.append(res)
        try:
            await interaction.edit_original_response(embed=busy_embed(f"계정 {개수}개 생성 중", i + 1, 개수))
        except discord.HTTPException:
            pass

    if not ok_results:
        try:
            await interaction.edit_original_response(embed=error_embed(fail_msg or "생성된 계정이 없습니다."))
        except discord.HTTPException:
            pass
        return

    cards = [code_embed(res["codes"], f"계정 #{i}") for i, res in enumerate(ok_results, 1)]
    txt = "\n".join(f"#{i}\t{r['codes'][0]}\t{r['codes'][1]}" for i, r in enumerate(ok_results, 1)).encode("utf-8")

    dm_ok = await send_dm(interaction.user, embeds=cards, files=[discord.File(io.BytesIO(txt), filename="accounts.txt")])

    try:
        if dm_ok:
            note = f"{len(ok_results)}개 생성했고 DM으로 보냈습니다."
            if fail_msg:
                note += f"\n중간에 중단됨: {fail_msg}"
            await interaction.edit_original_response(embed=discord.Embed(title="완료", description=note, color=Theme.OK))
        else:
            shown = cards[:9] if fail_msg else cards[:10]
            if fail_msg:
                shown.append(error_embed(f"{fail_msg}\n(성공 {len(ok_results)}개는 첨부 파일에 있습니다)"))
            shown[0].set_footer(text="DM을 보낼 수 없어 여기에 표시합니다. 전체 목록은 첨부 파일 확인")
            await interaction.edit_original_response(
                embeds=shown, attachments=[discord.File(io.BytesIO(txt), filename="accounts.txt")]
            )
    except discord.HTTPException as e:
        print(f"[AXIOM] 결과 전송 실패: {type(e).__name__}", file=sys.stderr)






@bot.event
async def on_ready():
    print(f"[AXIOM] 온라인: {bot.user}")






# ============================================================
# 관리 허브 (명령어는 /관리 하나)
# ============================================================
POST_MENU = [("에디터", "editor"), ("무료통조림", "free"), ("무료젠", "gen"), ("고양이 검색", "cat"),
             ("세이브 관리", "save"), ("이용 후기", "review"), ("랜덤 계정", "random"),
             ("문의 (티켓)", "ticket"), ("인증", "verify")]
PANEL_CHANNEL = {"editor": "에디터", "free": "무료통조림", "gen": "무료젠", "cat": "고양이-검색",
                 "save": "세이브관리", "review": "이용후기", "random": "랜덤계정",
                 "ticket": "문의하기", "verify": "인증"}
CHAT_OPEN = {"gen", "cat"}   # 이 채널들은 유저가 채팅을 칠 수 있어야 함


def gen_embed(gid: int) -> discord.Embed:
    c = gsec(gid, "free")
    cd = c["gen_cooldown_hours"]
    rule = "제한 없이 받을 수 있어요." if cd <= 0 else ("1인당 1회만 받을 수 있어요." if cd >= 24 * 365 else f"{cd}시간에 1회 받을 수 있어요.")
    e = discord.Embed(
        title="무료젠",
        description=(
            "이 채널에 아무 채팅이나 하나 치면 랜덤 계정을 DM으로 보내드려요.\n"
            f"확률은 {_pct(c['weights'])} 예요.\n\n"
            f"1. 무료젠은 {rule}\n"
            "2. DM을 열어두셔야 받을 수 있어요. 코드를 잘 보관해주세요.\n"
            "3. 부계정 사용을 금지합니다"
        ),
        color=Theme.MAIN,
    )
    e.set_author(name=SHOP_NAME)
    return e


def cat_guide_embed() -> discord.Embed:
    e = discord.Embed(title="고양이 ID 검색", description="이 채널에 고양이 이름만 치면 ID를 알려드려요.\n예) 트릭시", color=Theme.MAIN)
    e.set_author(name=SHOP_NAME)
    return e


async def ensure_panel_channel(guild: discord.Guild, kind: str, here) -> discord.TextChannel:
    """kind 전용 채널을 찾거나 만든다. 이전에 봇이 올린 메시지는 정리."""
    saved = gsec(guild.id, "panels").get(kind)
    ch = guild.get_channel(saved) if saved else None
    if not isinstance(ch, discord.TextChannel):
        everyone = discord.PermissionOverwrite(
            view_channel=True, send_messages=(kind in CHAT_OPEN), add_reactions=False,
            create_public_threads=False, create_private_threads=False)
        ow = {
            guild.default_role: everyone,
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True, manage_messages=True),
        }
        ch = await guild.create_text_channel(PANEL_CHANNEL[kind], category=getattr(here, "category", None),
                                             overwrites=ow, reason="패널 전용 채널")
        gsec(guild.id, "panels")[kind] = ch.id
        save_gcfg()
    else:
        try:
            def old_panel(m: discord.Message) -> bool:  # 후기·답장 같은 봇 메시지는 지우지 않음
                if m.author.id != guild.me.id:
                    return False
                return bool(m.components) or bool(m.embeds and m.embeds[0].title in ("무료젠", "고양이 ID 검색"))
            await ch.purge(limit=50, check=old_panel)
        except discord.HTTPException:
            pass
    return ch
SET_MENU = [("무료젠 · 검색 · 후기 설정", "free"), ("티켓", "ticket"), ("인증", "verify"),
            ("랜계 밸런스", "balance"), ("보안", "security")]


def build_post(kind: str, country: str, gid: int):
    if kind == "editor":
        return editor_embed(), EditorHub(country)
    if kind == "free":
        return free_embed(), QuickView(country)
    if kind == "gen":
        return gen_embed(gid), None
    if kind == "cat":
        return cat_guide_embed(), None
    if kind == "save":
        return save_embed(), SaveView(country)
    if kind == "review":
        return review_embed(), ReviewPanelView()
    if kind == "random":
        e = discord.Embed(title=f"{SHOP_NAME} 랜덤 계정", description="등급을 고르면 랜덤 계정을 DM으로 보내드려요.", color=Theme.ACCENT)
        return e, RandomPanelView()
    if kind == "ticket":
        e = discord.Embed(title=f"{SHOP_NAME} 문의", description="문의 종류를 고르면 1:1 채널이 열려요.", color=Theme.ACCENT)
        return e, TicketPanelView(gsec(gid, "ticket")["types"])
    e = discord.Embed(title=f"{SHOP_NAME} 인증", description="버튼을 누르면 인증이 끝나고 모든 채널을 볼 수 있어요.", color=Theme.ACCENT)
    return e, VerifyPanelView()


class SayModal(discord.ui.Modal, title="메시지 보내기"):
    heading = discord.ui.TextInput(label="제목 (비우면 일반 메시지)", required=False, max_length=100)
    body = discord.ui.TextInput(label="내용", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("보낼 채널을 찾을 수 없어요."), ephemeral=True)
            return
        no_everyone = discord.AllowedMentions(everyone=False, users=True, roles=True)
        try:
            if self.heading.value.strip():
                e = discord.Embed(title=self.heading.value.strip(), description=self.body.value, color=Theme.MAIN)
                await ch.send(embed=e, allowed_mentions=no_everyone)
            else:
                await ch.send(self.body.value, allowed_mentions=no_everyone)
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("보내지 못했어요. 봇 권한을 확인해주세요."), ephemeral=True)
            return
        await interaction.response.send_message(f"{ch.mention} 에 보냈어요.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[AXIOM] 메시지 보내기 오류: {type(error).__name__}", file=sys.stderr)


class SayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, country: str, target: int):
        kw = {"default_values": [discord.Object(id=target, type=discord.abc.GuildChannel)]} if target else {}
        super().__init__(placeholder="메시지 보낼 채널 (안 고르면 지금 채널)", channel_types=[discord.ChannelType.text],
                         min_values=1, max_values=1, row=3, **kw)
        self.country = country

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=AdminHub(self.country, self.values[0].id))


class AdminHub(discord.ui.View):
    def __init__(self, country: str = "kr", target: int = 0):
        super().__init__(timeout=600)
        self.country, self.target = country, target
        post = discord.ui.Select(placeholder="패널 올리기 (전용 채널 자동 생성)", row=0,
                                 options=[discord.SelectOption(label=n, value=k) for n, k in POST_MENU])
        post.callback = self.on_post
        sett = discord.ui.Select(placeholder="설정", row=1,
                                 options=[discord.SelectOption(label=n, value=k) for n, k in SET_MENU])
        sett.callback = self.on_set
        ctry = discord.ui.Select(placeholder=f"국가: {COUNTRIES[country]}", row=2,
                                 options=[discord.SelectOption(label=v, value=k, default=(k == country)) for k, v in COUNTRIES.items()])
        ctry.callback = self.on_country
        for it in (post, sett, ctry, SayChannelSelect(country, target)):
            self.add_item(it)
        say = discord.ui.Button(label="메시지 보내기", style=discord.ButtonStyle.primary, row=4)
        say.callback = self.on_say
        self.add_item(say)

    async def _reset(self, interaction: discord.Interaction):
        try:
            await interaction.edit_original_response(view=AdminHub(self.country, self.target))
        except discord.HTTPException:
            pass

    async def on_country(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=AdminHub(interaction.data["values"][0], self.target))

    async def on_say(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SayModal(self.target or interaction.channel_id))

    async def on_post(self, interaction: discord.Interaction):
        kind = interaction.data["values"][0]
        await interaction.response.defer()
        try:
            guild = interaction.guild
            ch = await ensure_panel_channel(guild, kind, interaction.channel)
            embed, view = build_post(kind, self.country, guild.id)
            await ch.send(embed=embed, **({"view": view} if view else {}))
            # 전용 채널을 기능에 자동 연결
            cfg = gsec(guild.id, "free")
            link = {"gen": "gen_channel", "cat": "cat_channel", "review": "review_channel"}.get(kind)
            if link and not (link == "review_channel" and cfg[link]):
                cfg[link] = ch.id
                save_gcfg()
            note = f"{ch.mention} 에 올렸어요."
        except discord.Forbidden:
            note = "채널을 만들 권한이 없어요. 봇에 `채널 관리`, `메시지 관리` 권한을 주세요."
        except discord.HTTPException:
            note = "채널을 만들거나 보내지 못했어요. 봇 권한을 확인해주세요."
        await interaction.followup.send(note, ephemeral=True)
        await self._reset(interaction)

    async def on_set(self, interaction: discord.Interaction):
        kind = interaction.data["values"][0]
        gid, uid = interaction.guild_id, interaction.user.id
        await interaction.response.defer()
        if kind in ("balance", "security") and uid not in ADMIN_IDS:
            await interaction.followup.send("봇 관리자만 열 수 있어요.", ephemeral=True)
        elif kind == "free":
            v = FreeSettings(gid)
            await interaction.followup.send(embed=v.embed(gid), view=v, ephemeral=True)
        elif kind == "ticket":
            v = TicketSettings(gid)
            await interaction.followup.send(embed=v.embed(gid), view=v, ephemeral=True)
        elif kind == "verify":
            v = VerifySettings(gid)
            await interaction.followup.send(embed=v.embed(gid), view=v, ephemeral=True)
        elif kind == "balance":
            v = BalancePanel(uid)
            await interaction.followup.send(embed=balance_embed(v.tier), view=v, ephemeral=True)
        else:
            await interaction.followup.send(embed=sec_embed(), view=SecurityPanel(uid), ephemeral=True)
        await self._reset(interaction)


@bot.tree.command(name="관리", description="패널 올리기 · 설정 (관리자 전용)")
@app_commands.guild_only()
async def admin_cmd(interaction: discord.Interaction):
    if not is_manager(interaction):
        await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    e = discord.Embed(title="관리", description="패널을 고르면 전용 채널이 만들어지고 거기에 올라가요.\n국가는 에디터 · 무료통조림 · 세이브 관리에만 쓰여요.", color=Theme.MAIN)
    await interaction.response.send_message(embed=e, view=AdminHub(), ephemeral=True)


@bot.tree.command(name="티켓추가", description="현재 티켓에 유저 추가")
@app_commands.guild_only()
async def ticket_add_cmd(interaction: discord.Interaction, 유저: discord.Member):
    if tk_info(interaction.channel) is None or not is_support(interaction):
        await interaction.response.send_message("티켓 채널에서 담당자만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.channel.set_permissions(
        유저, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    await interaction.response.send_message(f"{유저.mention} 님을 추가했습니다.")


@bot.tree.command(name="티켓제거", description="현재 티켓에서 유저 제거")
@app_commands.guild_only()
async def ticket_remove_cmd(interaction: discord.Interaction, 유저: discord.Member):
    info = tk_info(interaction.channel)
    if info is None or not is_support(interaction):
        await interaction.response.send_message("티켓 채널에서 담당자만 사용할 수 있습니다.", ephemeral=True)
        return
    if 유저.id == info[0]:
        await interaction.response.send_message("개설자는 제거할 수 없습니다.", ephemeral=True)
        return
    await interaction.channel.set_permissions(유저, overwrite=None)
    await interaction.response.send_message(f"{유저.mention} 님을 제거했습니다.")








if __name__ == "__main__":
    print("[AXIOM] 시작 중...")
    init_core()
    load_gcfg()
    load_free()
    try:
        bot.run(load_token())
    except discord.PrivilegedIntentsRequired:
        print("[AXIOM] 개발자 포털 > Bot > Privileged Gateway Intents 에서 MESSAGE CONTENT INTENT 를 켜고 다시 실행하세요.")