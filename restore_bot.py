# -*- coding: utf-8 -*-
"""
Axiom Restore Bot : 서버 백업 / 복구 / 디자인 봇

명령어 (전부 관리자 전용)
  /백업 [이름]        현재 서버 구조(역할, 카테고리, 채널, 권한)를 저장
  /백업목록            저장된 백업 보기
  /백업삭제 백업       백업 삭제
  /가져오기 파일       /백업 때 받은 json 파일을 올려서 백업으로 등록
  /복구 백업 모드      백업을 이 서버에 복구 (추가 / 초기화 후 복구)
  /디자인 스타일 오픈일   스크린샷 같은 꾸민 서버 템플릿을 한 번에 생성
  /꾸미기 스타일       지금 있는 카테고리 / 채널 이름을 꾸민 스타일로 바꿈
  /자동백업            이 서버 자동 백업 켜기/끄기 (6시간마다, 최근 10개 유지)
  /중지                진행 중인 복구/디자인 작업 중지

필요한 것 : discord.py >= 2.3, 봇 권한 Administrator (또는 채널/역할 관리)
토큰       : restore_token.txt 한 줄 (또는 환경변수 RESTORE_TOKEN)
"""
import asyncio
import io
import json
import logging
import os
import re
import time
import datetime as dt
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import tasks

# ───────────────────────── 설정 ─────────────────────────
BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / "backups"
CONFIG_FILE = BASE / "restore_config.json"
TOKEN_FILE = BASE / "restore_token.txt"
LOG_FILE = BASE / "restore_bot.log"

KEEP_PER_GUILD = 10          # 서버당 보관할 백업 수
AUTO_HOURS = 6               # 자동백업 주기(시간)
STEP_DELAY = 0.35            # 생성/삭제 사이 대기(초) - 레이트리밋 완화
MAX_ROLES = 250
MAX_CHANNELS = 500
SCHEMA = 1
COLOR = 0x5865F2
OK_COLOR = 0x57F287
BAD_COLOR = 0xED4245

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("restore")

RUNNING: set = set()              # 작업 중인 길드 id
CANCEL: Dict[int, bool] = {}      # 중지 요청


class Cancelled(Exception):
    pass


# ───────────────────────── 파일 유틸 ─────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"auto": {}}


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))


def read_token() -> str:
    tok = os.environ.get("RESTORE_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not tok:
        raise SystemExit("restore_token.txt 에 봇 토큰을 넣어줘.")
    return tok


# ───────────────────────── 스냅샷(백업) ─────────────────────────
def _kind(ch) -> Optional[str]:
    if isinstance(ch, discord.StageChannel):
        return "stage"
    if isinstance(ch, discord.VoiceChannel):
        return "voice"
    if isinstance(ch, discord.ForumChannel):
        return "forum"
    if isinstance(ch, discord.TextChannel):
        return "news" if ch.is_news() else "text"
    return None


def _ow_list(overwrites: dict) -> List[dict]:
    out = []
    for target, ow in overwrites.items():
        if not isinstance(target, discord.Role):
            continue                    # 멤버별 권한은 새 서버에서 의미 없음
        allow, deny = ow.pair()
        if not allow.value and not deny.value:
            continue
        out.append({
            "role": "@everyone" if target.is_default() else target.name,
            "allow": allow.value,
            "deny": deny.value,
        })
    return out


def snapshot(guild: discord.Guild, by: int, label: str = "") -> dict:
    roles = []
    for r in sorted(guild.roles, key=lambda x: x.position):
        if r.is_default() or r.managed:
            continue
        roles.append({
            "name": r.name, "color": r.colour.value, "hoist": r.hoist,
            "mentionable": r.mentionable, "permissions": r.permissions.value,
        })

    cats, chans = [], []
    cat_idx: Dict[int, int] = {}
    for i, c in enumerate(guild.categories):
        cat_idx[c.id] = i
        cats.append({"name": c.name, "overwrites": _ow_list(c.overwrites)})

    def add(ch, cat_i):
        k = _kind(ch)
        if not k:
            return
        chans.append({
            "kind": k, "name": ch.name, "cat": cat_i,
            "topic": getattr(ch, "topic", None),
            "nsfw": bool(getattr(ch, "nsfw", False)),
            "slowmode": int(getattr(ch, "slowmode_delay", 0) or 0),
            "bitrate": getattr(ch, "bitrate", None),
            "user_limit": int(getattr(ch, "user_limit", 0) or 0),
            "overwrites": _ow_list(ch.overwrites),
        })

    for c in guild.categories:
        for ch in c.channels:
            add(ch, cat_idx[c.id])
    loose = [c for c in guild.channels
             if c.category is None and not isinstance(c, discord.CategoryChannel)]
    for ch in sorted(loose, key=lambda x: (isinstance(x, discord.VoiceChannel), x.position)):
        add(ch, None)

    return {
        "meta": {
            "schema": SCHEMA, "guild_id": guild.id, "guild_name": guild.name,
            "by": by, "label": label[:60],
            "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "everyone_perms": guild.default_role.permissions.value,
        "roles": roles, "categories": cats, "channels": chans,
    }


def validate(data: Any) -> Optional[str]:
    """백업 json 검증. 문제 없으면 None, 있으면 이유 문자열."""
    try:
        if not isinstance(data, dict) or data.get("meta", {}).get("schema") != SCHEMA:
            return "이 봇에서 만든 백업 파일이 아니야."
        for key in ("roles", "categories", "channels"):
            if not isinstance(data.get(key), list):
                return f"'{key}' 항목이 없어."
        if len(data["roles"]) > MAX_ROLES or len(data["channels"]) > MAX_CHANNELS:
            return "역할/채널 수가 한도를 넘었어."
        for r in data["roles"]:
            if not isinstance(r.get("name"), str) or not 0 < len(r["name"]) <= 100:
                return "역할 이름이 이상해."
        for c in data["channels"]:
            if c.get("kind") not in ("text", "news", "voice", "stage", "forum"):
                return "채널 종류가 이상해."
            if not isinstance(c.get("name"), str) or not 0 < len(c["name"]) <= 100:
                return "채널 이름이 이상해."
        for c in data["categories"]:
            if not isinstance(c.get("name"), str) or not 0 < len(c["name"]) <= 100:
                return "카테고리 이름이 이상해."
        return None
    except Exception as e:  # noqa
        return f"검증 실패: {e}"


# ───────────────────────── 백업 저장소 ─────────────────────────
def save_backup(data: dict) -> Path:
    gid = data["meta"]["guild_id"]
    ts = int(time.time())
    path = BACKUP_DIR / f"{gid}_{ts}.json"
    n = 0
    while path.exists():
        n += 1
        path = BACKUP_DIR / f"{gid}_{ts}_{n}.json"
    _atomic_write(path, json.dumps(data, ensure_ascii=False))
    return path


def prune(guild_id: int) -> None:
    files = sorted(BACKUP_DIR.glob(f"{guild_id}_*.json"), key=lambda p: p.stat().st_mtime)
    for p in files[:-KEEP_PER_GUILD]:
        try:
            p.unlink()
        except OSError:
            pass


def list_backups(user_id: int, guild_id: Optional[int]) -> List[tuple]:
    """(Path, meta) 목록. 이 서버 백업이거나 내가 만든 백업만 보여줌."""
    out = []
    if not BACKUP_DIR.exists():
        return out
    for p in sorted(BACKUP_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
        except Exception:
            continue
        if meta.get("guild_id") == guild_id or meta.get("by") == user_id:
            out.append((p, meta))
    return out


def load_backup(name: str, user_id: int, guild_id: Optional[int]) -> Optional[dict]:
    name = os.path.basename(name)          # 경로 조작 방지
    for p, _ in list_backups(user_id, guild_id):
        if p.name == name:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def backup_label(p: Path, meta: dict) -> str:
    when = meta.get("created", "")[:16].replace("T", " ")
    tag = f" · {meta['label']}" if meta.get("label") else ""
    return f"{meta.get('guild_name', '?')} · {when}{tag}"


# ───────────────────────── 진행 표시 ─────────────────────────
class Progress:
    def __init__(self, interaction: discord.Interaction, title: str):
        self.i = interaction
        self.title = title
        self.msg: Optional[discord.Message] = None
        self.last = 0.0

    def _embed(self, text: str, done: int, total: int, color: int = COLOR) -> discord.Embed:
        pct = 0 if total <= 0 else min(100, int(done * 100 / total))
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        e = discord.Embed(title=self.title, description=f"`{bar}` **{pct}%**\n{text}", color=color)
        e.set_footer(text=f"{done}/{total} · 멈추려면 /중지")
        return e

    async def start(self, text: str = "준비 중…") -> None:
        try:
            self.msg = await self.i.original_response()
            await self.msg.edit(embed=self._embed(text, 0, 1), view=None)
        except discord.HTTPException:
            self.msg = None

    async def move_to(self, channel: discord.abc.Messageable) -> None:
        """초기화 복구 때 원래 채널이 지워지므로 새 채널로 진행창을 옮김."""
        try:
            self.msg = await channel.send(embed=self._embed("진행창을 옮겼어.", 0, 1))
        except discord.HTTPException:
            self.msg = None

    async def update(self, text: str, done: int, total: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < 2.5:
            return
        self.last = now
        if self.msg:
            try:
                await self.msg.edit(embed=self._embed(text, done, total))
            except discord.HTTPException:
                pass

    async def finish(self, embed: discord.Embed) -> None:
        if self.msg:
            try:
                await self.msg.edit(embed=embed, view=None)
                return
            except discord.HTTPException:
                pass
        try:
            await self.i.user.send(embed=embed)
        except discord.HTTPException:
            pass


# ───────────────────────── 복구 엔진 ─────────────────────────
class Builder:
    """역할/카테고리/채널을 순서대로 만들어 주는 공용 엔진 (복구 + 디자인 공용)."""

    def __init__(self, guild: discord.Guild, prog: Progress, reason: str):
        self.guild = guild
        self.prog = prog
        self.reason = reason
        self.errors: List[str] = []
        self.done = 0
        self.total = 1
        self.rolemap: Dict[str, discord.Role] = {}
        self.cats: Dict[int, discord.CategoryChannel] = {}
        self.created = {"roles": 0, "categories": 0, "channels": 0, "deleted": 0}

    def _check(self) -> None:
        if CANCEL.get(self.guild.id):
            raise Cancelled()

    async def step(self, label: str, factory: Callable, counter: Optional[str] = None):
        self._check()
        result = None
        try:
            result = await factory()
            if counter:
                self.created[counter] += 1
        except discord.Forbidden:
            self.errors.append(f"권한 부족 · {label}")
        except discord.HTTPException as e:
            self.errors.append(f"{label} · {(e.text or str(e.status))[:80]}")
        except Exception as e:  # noqa
            self.errors.append(f"{label} · {type(e).__name__}")
            log.exception("step failed: %s", label)
        self.done += 1
        await self.prog.update(label, self.done, self.total)
        await asyncio.sleep(STEP_DELAY)
        return result

    def overwrites(self, items: List[dict]) -> dict:
        out = {}
        for it in items or []:
            role = self.guild.default_role if it["role"] == "@everyone" else self.rolemap.get(it["role"])
            if role is None:
                continue
            out[role] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(it["allow"])), discord.Permissions(int(it["deny"])))
        return out

    async def roles(self, roles: List[dict]) -> None:
        for r in self.guild.roles:
            self.rolemap.setdefault(r.name, r)
        for r in reversed(roles):
            if r["name"] in self.rolemap:
                self.done += 1
                continue
            role = await self.step(
                f"역할 · {r['name']}",
                lambda r=r: self.guild.create_role(
                    name=r["name"], permissions=discord.Permissions(int(r["permissions"])),
                    colour=discord.Colour(int(r["color"])), hoist=bool(r["hoist"]),
                    mentionable=bool(r["mentionable"]), reason=self.reason),
                "roles")
            if role:
                self.rolemap[role.name] = role

    async def categories(self, cats: List[dict]) -> None:
        for i, c in enumerate(cats):
            cat = await self.step(
                f"카테고리 · {c['name']}",
                lambda c=c: self.guild.create_category(
                    c["name"], overwrites=self.overwrites(c.get("overwrites")), reason=self.reason),
                "categories")
            if cat:
                self.cats[i] = cat

    async def channels(self, chans: List[dict]) -> None:
        g = self.guild
        for c in chans:
            cat = self.cats.get(c["cat"]) if c.get("cat") is not None else None
            ow = self.overwrites(c.get("overwrites"))
            kw: Dict[str, Any] = {"overwrites": ow, "reason": self.reason}
            if cat:
                kw["category"] = cat
            k = c["kind"]
            topic = (c.get("topic") or None)
            if k in ("text", "news"):
                extra = {"news": True} if k == "news" else {}
                f = lambda c=c, kw=kw, extra=extra, topic=topic: g.create_text_channel(
                    c["name"], topic=topic, nsfw=bool(c.get("nsfw")),
                    slowmode_delay=int(c.get("slowmode") or 0), **extra, **kw)
            elif k == "voice":
                br = min(int(c.get("bitrate") or 64000), g.bitrate_limit)
                f = lambda c=c, kw=kw, br=br: g.create_voice_channel(
                    c["name"], bitrate=br, user_limit=int(c.get("user_limit") or 0), **kw)
            elif k == "stage":
                f = lambda c=c, kw=kw: g.create_stage_channel(c["name"], **kw)
            else:
                f = lambda c=c, kw=kw, topic=topic: g.create_forum(
                    c["name"], topic=topic, nsfw=bool(c.get("nsfw")), **kw)
            ch = await self.step(f"채널 · {c['name']}", f, "channels")
            if ch is None and k == "news":
                await self.step(f"채널(일반) · {c['name']}",
                                lambda c=c, kw=kw, topic=topic: g.create_text_channel(
                                    c["name"], topic=topic, **kw), "channels")


async def run_restore(interaction: discord.Interaction, data: dict, wipe: bool) -> None:
    guild = interaction.guild
    prog = Progress(interaction, "🛠️ 서버 복구 진행 중")
    await prog.start()
    b = Builder(guild, prog, f"복구 by {interaction.user}")
    RUNNING.add(guild.id)
    CANCEL.pop(guild.id, None)
    t0 = time.monotonic()
    cancelled = False
    try:
        roles, cats, chans = data["roles"], data["categories"], data["channels"]
        b.total = len(roles) + len(cats) + len(chans) + 1
        if wipe:
            b.total += len(guild.channels) + len(guild.roles)
            keep = None
            try:
                keep = await guild.create_text_channel(
                    "복구로그", overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
                    reason="복구 진행창")
                await prog.move_to(keep)
            except discord.HTTPException:
                pass
            for ch in list(guild.channels):
                if keep and ch.id == keep.id:
                    continue
                await b.step(f"삭제 · {ch.name}", lambda ch=ch: ch.delete(reason="복구 초기화"), "deleted")
            me_top = guild.me.top_role
            for r in list(guild.roles):
                if r.is_default() or r.managed or r >= me_top:
                    continue
                await b.step(f"역할 삭제 · {r.name}", lambda r=r: r.delete(reason="복구 초기화"), "deleted")
            b.rolemap.clear()
        if data.get("everyone_perms") is not None:
            await b.step("@everyone 권한", lambda: guild.default_role.edit(
                permissions=discord.Permissions(int(data["everyone_perms"])), reason=b.reason))
        await b.roles(roles)
        await b.categories(cats)
        await b.channels(chans)
    except Cancelled:
        cancelled = True
    finally:
        RUNNING.discard(guild.id)
        CANCEL.pop(guild.id, None)

    took = int(time.monotonic() - t0)
    c = b.created
    e = discord.Embed(
        title="⛔ 복구 중지됨" if cancelled else ("✅ 복구 완료" if not b.errors else "⚠️ 복구 완료 (일부 실패)"),
        color=BAD_COLOR if cancelled else (OK_COLOR if not b.errors else 0xFEE75C))
    e.add_field(name="생성", value=f"역할 {c['roles']} · 카테고리 {c['categories']} · 채널 {c['channels']}", inline=False)
    if c["deleted"]:
        e.add_field(name="삭제", value=f"{c['deleted']}개", inline=False)
    if b.errors:
        e.add_field(name=f"실패 {len(b.errors)}건", value="\n".join(b.errors[:8])[:1000], inline=False)
    e.set_footer(text=f"{took}초 걸림 · 봇 역할을 맨 위로 올리면 실패가 줄어들어"
                    + (" · 복구로그 채널은 지워도 돼" if wipe else ""))
    await prog.finish(e)
    try:
        await interaction.user.send(embed=e)
    except discord.HTTPException:
        pass


# ───────────────────────── 디자인 템플릿 ─────────────────────────
TEMPLATE = [
    ("📊", "서버", [("locked", "", "")]),
    ("✅", "인증", [("text", "✅", "인증")]),
    ("🍀", "시스템", [("text", "👋", "입장로그")]),
    ("📢", "서버안내", [
        ("text", "🎀", "서버안내"), ("text", "⏰", "서버공지"), ("text", "🌼", "서버규칙"),
        ("text", "🌏", "서버링크"), ("text", "🐸", "서버역할"), ("text", "🔔", "코드안내"),
        ("text", "🎁", "이벤트")]),
    ("🎁", "무료커스텀", [
        ("text", "❤️", "무료커스텀안내"), ("text", "💎", "커스텀하기"), ("text", "🔍", "고양이검색"),
        ("text", "🍒", "커스텀후기"), ("text", "⚙️", "봇사용하기"), ("text", "⚙️", "봇설명서")]),
    ("💰", "문의하기", [
        ("text", "🔥", "문의"), ("text", "🌸", "회원획득"), ("text", "🏆", "명예의전당")]),
    ("💬", "커뮤니티", [
        ("text", "🍀", "자유채팅"), ("text", "🤡", "회원채팅"), ("text", "🚀", "VIP채팅"),
        ("text", "🍬", "명령어방"), ("forum", "❓", "질문답변"), ("text", "🔤", "끝말잇기"),
        ("voice", "☎️", "음성통화")]),
]

STYLES = {
    "1": ("╔═════[ {e} {n} {e} ]═════╗", "『{e}』|{n}"),
    "2": ("┏━━━━[ {e} {n} {e} ]━━━━┓", "「{e}」{n}"),
    "3": ("━━━━ {e} {n} {e} ━━━━", "{e}┃{n}"),
    "4": ("【 {e} {n} {e} 】", "｜{e}｜{n}"),
}
STYLE_CHOICES = [
    app_commands.Choice(name="1) ╔═══[ 이모지 이름 ]═══╗  『🎀』|이름", value="1"),
    app_commands.Choice(name="2) ┏━━━[ 이모지 이름 ]━━━┓  「🎀」이름", value="2"),
    app_commands.Choice(name="3) ━━━ 이모지 이름 ━━━  🎀┃이름", value="3"),
    app_commands.Choice(name="4) 【 이모지 이름 】  ｜🎀｜이름", value="4"),
]

_DECOR = re.compile(r"[╔╗╚╝═║━┃┏┓┗┛─│\[\]『』「」【】｜|<>•·_]+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️‍]")
KEYWORDS = [
    ("공지", "⏰"), ("규칙", "🌼"), ("인증", "✅"), ("입장", "👋"), ("로그", "📋"), ("이벤트", "🎁"),
    ("자유", "🍀"), ("채팅", "💬"), ("문의", "🔥"), ("후기", "🍒"), ("음성", "🔊"), ("통화", "☎️"),
    ("안내", "🎀"), ("링크", "🌏"), ("역할", "🐸"), ("코드", "🔔"), ("봇", "⚙️"), ("검색", "🔍"),
    ("커스텀", "💎"), ("명령", "🍬"), ("질문", "❓"), ("서버", "📊"), ("시스템", "🍀"), ("게임", "🎮"),
    ("음악", "🎵"), ("사진", "📷"), ("랭킹", "🏆"), ("건의", "📩"), ("신고", "🚨"), ("파티", "🎉"),
]


def core_name(s: str) -> str:
    s = _EMOJI.sub("", s)
    s = _DECOR.sub(" ", s)
    s = re.sub(r"[\s\-]+", " ", s).strip()
    return s or "채널"


def pick_emoji(name: str, fallback: str = "✨") -> str:
    for key, emo in KEYWORDS:
        if key in name:
            return emo
    return fallback


def style_cat(style: str, emoji: str, name: str) -> str:
    return STYLES[style][0].format(e=emoji, n=name)[:100]


def style_ch(style: str, emoji: str, name: str, spaced: bool = False) -> str:
    n = name if spaced else name.replace(" ", "-")
    return STYLES[style][1].format(e=emoji, n=n)[:100]


def build_template(style: str, open_date: str) -> tuple:
    cats, chans = [], []
    for i, (cemo, cname, items) in enumerate(TEMPLATE):
        cats.append({"name": style_cat(style, cemo, cname), "overwrites": []})
        for kind, emo, name in items:
            if kind == "locked":
                chans.append({
                    "kind": "voice", "name": f"『KR』Open : {open_date}"[:100], "cat": i,
                    "overwrites": [{"role": "@everyone",
                                    "allow": discord.Permissions(view_channel=True).value,
                                    "deny": discord.Permissions(connect=True).value}],
                    "bitrate": 64000, "user_limit": 0})
            elif kind == "voice":
                chans.append({
                    "kind": "voice", "name": style_ch(style, emo, name, spaced=True), "cat": i,
                    "overwrites": [], "bitrate": 64000, "user_limit": 99})
            else:
                chans.append({
                    "kind": kind, "name": style_ch(style, emo, name), "cat": i,
                    "overwrites": [], "topic": None, "nsfw": False, "slowmode": 0})
    return cats, chans


# ───────────────────────── 확인 버튼 ─────────────────────────
class Confirm(discord.ui.View):
    def __init__(self, user_id: int, ok_label: str = "시작", danger: bool = True):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.value: Optional[bool] = None
        self.go.label = ok_label
        self.go.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("명령어를 쓴 사람만 누를 수 있어.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="시작", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


async def ask(interaction: discord.Interaction, embed: discord.Embed, ok: str, danger: bool = True) -> bool:
    view = Confirm(interaction.user.id, ok, danger)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    if view.value is not True:
        try:
            await interaction.edit_original_response(
                embed=discord.Embed(description="취소됐어.", color=BAD_COLOR), view=None)
        except discord.HTTPException:
            pass
        return False
    return True


# ───────────────────────── 봇 본체 ─────────────────────────
class RestoreBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        BACKUP_DIR.mkdir(exist_ok=True)
        await self.tree.sync()
        auto_backup.start()

    async def on_ready(self):
        log.info("로그인: %s (%s개 서버)", self.user, len(self.guilds))
        await self.change_presence(activity=discord.Game("/백업 · /복구 · /디자인"))


bot = RestoreBot()
tree = bot.tree
ADMIN = app_commands.default_permissions(administrator=True)


def is_admin(i: discord.Interaction) -> bool:
    return bool(i.guild) and (i.user.guild_permissions.administrator or i.user.id == i.guild.owner_id)


async def deny(i: discord.Interaction, msg: str = "관리자만 쓸 수 있어.") -> None:
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


def bot_ready(i: discord.Interaction) -> Optional[str]:
    p = i.guild.me.guild_permissions
    if not (p.administrator or (p.manage_channels and p.manage_roles)):
        return "봇에게 관리자 권한(또는 채널 관리 + 역할 관리)이 필요해."
    if i.guild.id in RUNNING:
        return "이미 작업이 진행 중이야. 끝나길 기다리거나 /중지 를 써줘."
    return None


@tree.command(name="백업", description="현재 서버 구조(역할·카테고리·채널·권한)를 저장해요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(label="이름")
@app_commands.describe(label="백업 이름(선택)")
async def cmd_backup(i: discord.Interaction, label: str = ""):
    if not is_admin(i):
        return await deny(i)
    await i.response.defer(ephemeral=True)
    data = snapshot(i.guild, i.user.id, label)
    path = save_backup(data)
    prune(i.guild.id)
    e = discord.Embed(title="💾 백업 완료", color=OK_COLOR)
    e.add_field(name="저장 내용", value=f"역할 {len(data['roles'])} · 카테고리 {len(data['categories'])} · 채널 {len(data['channels'])}")
    e.set_footer(text="첨부된 json 파일도 따로 보관해 둬. 봇이 사라져도 /가져오기 로 살릴 수 있어.")
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await i.followup.send(embed=e, file=discord.File(io.BytesIO(raw), filename=path.name), ephemeral=True)


@tree.command(name="백업목록", description="저장된 백업 목록")
@app_commands.guild_only()
@ADMIN
async def cmd_list(i: discord.Interaction):
    if not is_admin(i):
        return await deny(i)
    items = list_backups(i.user.id, i.guild_id)
    if not items:
        return await i.response.send_message("저장된 백업이 없어. /백업 으로 만들어줘.", ephemeral=True)
    lines = []
    for p, m in items[:20]:
        lines.append(f"• `{p.name}`\n  {backup_label(p, m)}")
    e = discord.Embed(title="📦 백업 목록", description="\n".join(lines)[:4000], color=COLOR)
    await i.response.send_message(embed=e, ephemeral=True)


async def backup_ac(i: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    out = []
    for p, m in list_backups(i.user.id, i.guild_id):
        label = backup_label(p, m)
        if current.lower() in label.lower() or current in p.name:
            out.append(app_commands.Choice(name=label[:100], value=p.name))
        if len(out) >= 25:
            break
    return out


@tree.command(name="백업삭제", description="백업 하나를 삭제해요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(name="백업")
@app_commands.autocomplete(name=backup_ac)
async def cmd_del(i: discord.Interaction, name: str):
    if not is_admin(i):
        return await deny(i)
    if load_backup(name, i.user.id, i.guild_id) is None:
        return await i.response.send_message("그 백업을 못 찾았어.", ephemeral=True)
    try:
        (BACKUP_DIR / os.path.basename(name)).unlink()
    except OSError:
        return await i.response.send_message("삭제에 실패했어.", ephemeral=True)
    await i.response.send_message("🗑️ 삭제했어.", ephemeral=True)


@tree.command(name="가져오기", description="/백업 때 받은 json 파일을 올려서 백업으로 등록해요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(file="파일")
async def cmd_import(i: discord.Interaction, file: discord.Attachment):
    if not is_admin(i):
        return await deny(i)
    if file.size > 8 * 1024 * 1024:
        return await i.response.send_message("파일이 너무 커.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        return await i.followup.send("json 파일을 읽을 수 없어.", ephemeral=True)
    why = validate(data)
    if why:
        return await i.followup.send(f"❌ {why}", ephemeral=True)
    data["meta"]["by"] = i.user.id
    data["meta"]["label"] = (data["meta"].get("label") or "가져옴")[:60]
    path = save_backup(data)
    await i.followup.send(f"✅ 등록 완료 · `{path.name}`\n이제 /복구 에서 고를 수 있어.", ephemeral=True)


@tree.command(name="복구", description="백업을 이 서버에 복구해요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(name="백업", mode="모드")
@app_commands.autocomplete(name=backup_ac)
@app_commands.choices(mode=[
    app_commands.Choice(name="추가 (기존 채널·역할 유지하고 없는 것만 만들기)", value="add"),
    app_commands.Choice(name="초기화 후 복구 (기존 채널·역할 전부 삭제, 서버 주인만)", value="wipe"),
])
async def cmd_restore(i: discord.Interaction, name: str, mode: app_commands.Choice[str]):
    if not is_admin(i):
        return await deny(i)
    wipe = mode.value == "wipe"
    if wipe and i.user.id != i.guild.owner_id:
        return await deny(i, "초기화 복구는 서버 주인만 할 수 있어.")
    why = bot_ready(i)
    if why:
        return await deny(i, why)
    data = load_backup(name, i.user.id, i.guild_id)
    if data is None or validate(data):
        return await deny(i, "그 백업을 못 찾았거나 파일이 깨졌어.")
    m = data["meta"]
    e = discord.Embed(
        title="🔥 초기화 후 복구" if wipe else "🛠️ 추가 복구",
        description=(
            f"**{m.get('guild_name', '?')}** 백업을 이 서버에 적용해요.\n"
            f"역할 {len(data['roles'])} · 카테고리 {len(data['categories'])} · 채널 {len(data['channels'])}\n\n"
            + ("⚠️ **지금 있는 모든 채널과 (봇보다 아래의) 역할이 삭제돼요. 되돌릴 수 없어요.**" if wipe
               else "기존 것은 건드리지 않고 없는 역할만 만들고 채널은 새로 추가해요.")),
        color=BAD_COLOR if wipe else COLOR)
    if not await ask(i, e, "복구 시작", danger=wipe):
        return
    await run_restore(i, data, wipe)


@tree.command(name="디자인", description="꾸민 서버 템플릿(카테고리·채널)을 한 번에 만들어요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(style="스타일", open_date="오픈일")
@app_commands.describe(open_date="맨 위 잠긴 음성 채널에 표시할 오픈일 (예: 2026.10.25)")
@app_commands.choices(style=STYLE_CHOICES)
async def cmd_design(i: discord.Interaction, style: app_commands.Choice[str], open_date: str = ""):
    if not is_admin(i):
        return await deny(i)
    why = bot_ready(i)
    if why:
        return await deny(i, why)
    open_date = (open_date or dt.date.today().strftime("%Y.%m.%d"))[:30]
    cats, chans = build_template(style.value, open_date)
    sample = "\n".join(c["name"] for c in cats[:4])
    e = discord.Embed(
        title="🎨 디자인 템플릿 생성",
        description=f"카테고리 {len(cats)}개 · 채널 {len(chans)}개를 새로 만들어요. 기존 채널은 건드리지 않아요.\n```\n{sample}\n…```",
        color=COLOR)
    if not await ask(i, e, "만들기", danger=False):
        return
    data = {"meta": {"guild_name": "템플릿"}, "roles": [], "categories": cats, "channels": chans,
            "everyone_perms": None}
    await run_restore(i, data, wipe=False)


@tree.command(name="꾸미기", description="지금 있는 카테고리·채널 이름을 꾸민 스타일로 바꿔요")
@app_commands.guild_only()
@ADMIN
@app_commands.rename(style="스타일")
@app_commands.choices(style=STYLE_CHOICES)
async def cmd_restyle(i: discord.Interaction, style: app_commands.Choice[str]):
    if not is_admin(i):
        return await deny(i)
    why = bot_ready(i)
    if why:
        return await deny(i, why)
    g = i.guild
    plan = []
    for c in g.categories:
        name = core_name(c.name)
        plan.append((c, style_cat(style.value, pick_emoji(name, "📌"), name)))
        for ch in c.channels:
            if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
                n = core_name(ch.name)
                plan.append((ch, style_ch(style.value, pick_emoji(n), n)))
    plan = [(ch, nn) for ch, nn in plan if ch.name != nn]
    if not plan:
        return await deny(i, "바꿀 이름이 없어.")
    sample = "\n".join(f"{ch.name} → {nn}" for ch, nn in plan[:5])
    e = discord.Embed(
        title="✏️ 이름 꾸미기",
        description=f"{len(plan)}개의 이름을 바꿔요 (음성 채널은 그대로).\n```\n{sample}\n…```",
        color=COLOR)
    if not await ask(i, e, "바꾸기", danger=False):
        return
    prog = Progress(i, "✏️ 이름 바꾸는 중")
    await prog.start()
    b = Builder(g, prog, f"꾸미기 by {i.user}")
    b.total = len(plan)
    RUNNING.add(g.id)
    try:
        for ch, nn in plan:
            await b.step(f"이름 변경 · {nn}", lambda ch=ch, nn=nn: ch.edit(name=nn, reason=b.reason))
    finally:
        RUNNING.discard(g.id)
        CANCEL.pop(g.id, None)
    
    e_res = discord.Embed(title="✨ 이름 꾸미기 완료", description=f"총 {b.done}개 중 {len(b.errors)}건 실패", color=OK_COLOR)
    if b.errors:
        e_res.add_field(name="실패 목록", value="\n".join(b.errors[:8])[:1000])
    await prog.finish(e_res)


@tree.command(name="자동백업", description="이 서버의 자동 백업을 켜거나 끕니다")
@app_commands.guild_only()
@ADMIN
@app_commands.choices(state=[
    app_commands.Choice(name="켜기 (6시간마다)", value="on"),
    app_commands.Choice(name="끄기", value="off"),
])
async def cmd_auto(i: discord.Interaction, state: app_commands.Choice[str]):
    if not is_admin(i):
        return await deny(i)
    cfg = load_config()
    auto_dict = cfg.setdefault("auto", {})
    if state.value == "on":
        auto_dict[str(i.guild_id)] = True
        save_config(cfg)
        await i.response.send_message("✅ 자동 백업이 활성화되었어요. (6시간 주기)", ephemeral=True)
    else:
        auto_dict.pop(str(i.guild_id), None)
        save_config(cfg)
        await i.response.send_message("📴 자동 백업이 비활성화되었어요.", ephemeral=True)


@tree.command(name="중지", description="현재 서버에서 실행 중인 백업/복구/디자인 작업을 중지합니다")
@app_commands.guild_only()
@ADMIN
async def cmd_stop(i: discord.Interaction):
    if not is_admin(i):
        return await deny(i)
    if i.guild.id not in RUNNING:
        return await i.response.send_message("현재 진행 중인 작업이 없어.", ephemeral=True)
    CANCEL[i.guild.id] = True
    await i.response.send_message("🛑 작업 중지 요청을 보냈어요. 잠시만 기다려줘.", ephemeral=True)


@tasks.loop(hours=AUTO_HOURS)
async def auto_backup():
    cfg = load_config()
    auto_dict = cfg.get("auto", {})
    for guild_id_str, enabled in list(auto_dict.items()):
        if not enabled:
            continue
        guild = bot.get_guild(int(guild_id_str))
        if not guild:
            continue
        try:
            data = snapshot(guild, bot.user.id, "자동백업")
            save_backup(data)
            prune(guild.id)
            log.info("자동 백업 성공: %s (%s)", guild.name, guild.id)
        except Exception:
            log.exception("자동 백업 실패: %s", guild.id)


@auto_backup.before_loop
async def before_auto_backup():
    await bot.wait_until_ready()


if __name__ == "__main__":
    token = read_token()
    bot.run(token)