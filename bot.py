# bot.py
# ------------------------------------------------------------
# Discord 多人同步記帳機器人（v2）
# 重點：
# - 新增 /between：查兩位成員之間的款項狀況（任何人可查）
# - 只註冊 guild-level 指令（即時生效），並清空全域指令避免殘留
# - 保留 /owe、/paid、/balance、/history、/undo
# - 自動清理已下架指令名稱（/lent、/split_equal）
# ------------------------------------------------------------

import asyncio
import os
import re
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite

from discord.ext import tasks
from itertools import cycle

DB_PATH = os.getenv("LEDGER_DB", "ledger.db")
DEFAULT_CCY = os.getenv("DEFAULT_CCY", "TWD")
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.message_content = False
INTENTS.members = True
INTENTS.presences = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

SQL_INIT = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  creditor_id INTEGER NOT NULL,
  debtor_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  currency TEXT NOT NULL DEFAULT 'TWD',
  kind TEXT NOT NULL DEFAULT 'debt',
  note TEXT,
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_guild ON ledger(guild_id);
CREATE INDEX IF NOT EXISTS idx_ledger_pair ON ledger(guild_id, creditor_id, debtor_id);
CREATE INDEX IF NOT EXISTS idx_ledger_created_at ON ledger(created_at DESC);
"""

AMOUNT_RE = re.compile(r"^(?P<num>\d+(?:[.,]\d{1,2})?)$")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SQL_INIT)
        await db.commit()

async def parse_amount_to_cents(amount_str: str) -> int:
    amount_str = amount_str.strip()
    m = AMOUNT_RE.match(amount_str)
    if not m:
        raise ValueError("金額格式錯誤，請輸入例如 120 或 120.50")
    num = m.group('num').replace(',', '.')
    cents = int(round(float(num) * 100))
    if cents <= 0:
        raise ValueError("金額需大於 0")
    return cents

async def add_entry(guild_id: int, channel_id: int, creditor_id: int, debtor_id: int,
                    amount_cents: int, currency: str, kind: str, note: Optional[str], created_by: int) -> int:
    if creditor_id == debtor_id:
        raise ValueError("不可對自己記帳")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "INSERT INTO ledger (guild_id, channel_id, creditor_id, debtor_id, amount_cents, currency, kind, note, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (guild_id, channel_id, creditor_id, debtor_id, amount_cents, currency, kind, note or None, created_by)
        )
        await db.commit()
        return cur.lastrowid

async def pair_net_cents(guild_id: int, a_id: int, b_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur1 = await db.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE guild_id=? AND creditor_id=? AND debtor_id=?",
            (guild_id, a_id, b_id)
        )
        s1 = (await cur1.fetchone())[0]
        cur2 = await db.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE guild_id=? AND creditor_id=? AND debtor_id=?",
            (guild_id, b_id, a_id)
        )
        s2 = (await cur2.fetchone())[0]
        return s1 - s2

async def top_counterparties(guild_id: int, me_id: int, limit: int = 8) -> List[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        q = """
        WITH pairs AS (
          SELECT creditor_id AS a, debtor_id AS b, amount_cents FROM ledger WHERE guild_id=? AND (creditor_id=? OR debtor_id=?)
        )
        SELECT other_id,
               SUM(CASE WHEN role='recv' THEN amount_cents ELSE -amount_cents END) AS net
        FROM (
          SELECT b AS other_id, amount_cents, 'recv' AS role FROM pairs WHERE a = ?
          UNION ALL
          SELECT a AS other_id, amount_cents, 'pay' AS role FROM pairs WHERE b = ?
        )
        GROUP BY other_id
        HAVING net != 0
        ORDER BY ABS(net) DESC
        LIMIT ?
        """
        cur = await db.execute(q, (guild_id, me_id, me_id, me_id, me_id, limit))
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

async def recent_entries(guild_id: int, a_id: int, b_id: Optional[int], limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        if b_id:
            cur = await db.execute(
                """
                SELECT id, creditor_id, debtor_id, amount_cents, currency, kind, note, created_by, created_at
                FROM ledger WHERE guild_id=? AND (
                    (creditor_id=? AND debtor_id=?) OR (creditor_id=? AND debtor_id=?)
                )
                ORDER BY id DESC LIMIT ?
                """,
                (guild_id, a_id, b_id, b_id, a_id, limit)
            )
        else:
            cur = await db.execute(
                """
                SELECT id, creditor_id, debtor_id, amount_cents, currency, kind, note, created_by, created_at
                FROM ledger WHERE guild_id=? AND (creditor_id=? OR debtor_id=?)
                ORDER BY id DESC LIMIT ?
                """,
                (guild_id, a_id, a_id, limit)
            )
        return await cur.fetchall()

# 撤銷工具：刪除「此頻道」你自己上一筆
async def pop_last_entry(guild_id: int, channel_id: int, created_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """
            SELECT id, creditor_id, debtor_id, amount_cents, currency, kind, note, created_at
            FROM ledger
            WHERE guild_id=? AND channel_id=? AND created_by=?
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, channel_id, created_by)
        )
        row = await cur.fetchone()
        if not row:
            await db.rollback()
            return None
        await db.execute("DELETE FROM ledger WHERE id=?", (row[0],))
        await db.commit()
        return row

# --------------------- 斜線指令 ---------------------

@app_commands.guild_only()
@bot.tree.command(name="owe", description="我欠對方金額（建立債務）")
@app_commands.describe(user="對方（被欠錢的人）", amount="金額（例如 120 或 120.50）", note="備註")
async def owe(interaction: discord.Interaction, user: discord.Member, amount: str, note: Optional[str] = None):
    try:
        cents = await parse_amount_to_cents(amount)
        entry_id = await add_entry(
            guild_id=interaction.guild_id, channel_id=interaction.channel_id,
            creditor_id=user.id, debtor_id=interaction.user.id,
            amount_cents=cents, currency=DEFAULT_CCY, kind='debt', note=note, created_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ 已記錄：你欠 {user.mention} {cents/100:.2f} {DEFAULT_CCY}（# {entry_id}）" + (f"｜{note}" if note else "")
        )
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(name="paid", description="我已支付給對方（減少債務）")
@app_commands.describe(user="給錢的對象", amount="金額（例如 120 或 120.50）", note="備註")
async def paid(interaction: discord.Interaction, user: discord.Member, amount: str, note: Optional[str] = None):
    try:
        cents = await parse_amount_to_cents(amount)
        entry_id = await add_entry(
            guild_id=interaction.guild_id, channel_id=interaction.channel_id,
            creditor_id=interaction.user.id, debtor_id=user.id,
            amount_cents=cents, currency=DEFAULT_CCY, kind='payment', note=note or 'payment', created_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"💸 已記錄付款：{user.mention} ← {cents/100:.2f} {DEFAULT_CCY}（# {entry_id}）" + (f"｜{note}" if note else "")
        )
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(name="balance", description="查看與某人的淨額，或列出前幾名對手方")
@app_commands.describe(user="可選，指定對象則顯示雙方淨額")
async def balance(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    me = interaction.user
    if user and user.id == me.id:
        await interaction.response.send_message("🙂 自己與自己沒有債務。", ephemeral=True)
        return
    if user:
        net = await pair_net_cents(interaction.guild_id, me.id, user.id)
        if net == 0:
            txt = f"你與 {user.mention} 之間已結清。"
        elif net > 0:
            txt = f"{user.mention} 淨欠你 {net/100:.2f} {DEFAULT_CCY}"
        else:
            txt = f"你淨欠 {user.mention} {abs(net)/100:.2f} {DEFAULT_CCY}"
        await interaction.response.send_message(f"📊 {txt}")
    else:
        rows = await top_counterparties(interaction.guild_id, me.id, limit=8)
        if not rows:
            await interaction.response.send_message("📊 目前沒有未結清的對手方。", ephemeral=True)
            return
        lines = []
        for uid, net in rows:
            mention = f"<@{uid}>"
            lines.append(f"{mention} 淨欠你 {net/100:.2f} {DEFAULT_CCY}" if net > 0 else f"你淨欠 {mention} {abs(net)/100:.2f} {DEFAULT_CCY}")
        await interaction.response.send_message("📈 你的前幾名對手方：\n" + "\n".join(lines))

@app_commands.guild_only()
@bot.tree.command(name="history", description="查看最近的記錄")
@app_commands.describe(user="可選，限定與此人之間", limit="筆數，預設 10")
async def history(interaction: discord.Interaction, user: Optional[discord.Member] = None, limit: Optional[int] = 10):
    limit = max(1, min(50, limit or 10))
    rows = await recent_entries(interaction.guild_id, interaction.user.id, user.id if user else None, limit)
    if not rows:
        await interaction.response.send_message("📝 尚無記錄。", ephemeral=True)
        return
    def line(r):
        _id, cred, debt, cents, ccy, kind, note, created_by, created_at = r
        return f"#{_id} [{ccy}] {cents/100:.2f} {kind} | <@{debt}> → <@{cred}> | by <@{created_by}> | {created_at}" + (f" ｜{note}" if note else "")
    await interaction.response.send_message("🧾 最近記錄：\n" + "\n".join(line(r) for r in rows))

@app_commands.guild_only()
@bot.tree.command(name="undo", description="撤銷你在此頻道上一筆建立的記錄")
async def undo(interaction: discord.Interaction):
    row = await pop_last_entry(interaction.guild_id, interaction.channel_id, interaction.user.id)
    if not row:
        await interaction.response.send_message("↩️ 沒有可撤銷的記錄（此頻道中你尚未建立過記錄）。", ephemeral=True)
        return
    _id, cred, debt, cents, ccy, kind, note, created_at = row
    await interaction.response.send_message(
        "↩️ 已撤銷上一筆：\n"
        f"#{_id} [{ccy}] {cents/100:.2f} {kind} | <@{debt}> → <@{cred}> | {created_at}"
        + (f" ｜{note}" if note else "")
    )

# 新增：/between 查兩人款項狀況
@app_commands.guild_only()
@bot.tree.command(name="between", description="查詢兩位成員之間的款項狀況（任何人可查）")
@app_commands.describe(user_a="成員 A", user_b="成員 B", limit="附帶顯示最近筆數，預設 5")
async def between(interaction: discord.Interaction, user_a: discord.Member, user_b: discord.Member, limit: Optional[int] = 5):
    if user_a.id == user_b.id:
        await interaction.response.send_message("🙂 請選擇兩個不同的成員。", ephemeral=True)
        return

    net = await pair_net_cents(interaction.guild_id, user_a.id, user_b.id)
    if net == 0:
        header = f"✅ {user_a.mention} 與 {user_b.mention} 之間已結清。"
    elif net > 0:
        header = f"📊 {user_b.mention} 淨欠 {user_a.mention} **{net/100:.2f} {DEFAULT_CCY}**"
    else:
        header = f"📊 {user_a.mention} 淨欠 {user_b.mention} **{abs(net)/100:.2f} {DEFAULT_CCY}**"

    # 附帶最近紀錄
    limit = max(1, min(20, limit or 5))
    rows = await recent_entries(interaction.guild_id, user_a.id, user_b.id, limit)
    if not rows:
        await interaction.response.send_message(header + "\n（兩人之間尚無記錄）")
        return

    def line(r):
        _id, cred, debt, cents, ccy, kind, note, created_by, created_at = r
        return f"#{_id} [{ccy}] {cents/100:.2f} {kind} | <@{debt}> → <@{cred}> | by <@{created_by}> | {created_at}" + (f" ｜{note}" if note else "")

    body = "\n".join(line(r) for r in rows)
    await interaction.response.send_message(header + "\n🧾 最近記錄：\n" + body)

# --------------------- 指令清理 / 同步 ---------------------

REMOVED_CMD_NAMES = {"lent", "split_equal"}

async def _purge_removed_commands_for_guild(app_id: int, guild_id: int, http):
    try:
        guild_cmds = await http.get_guild_commands(app_id, guild_id)
        for c in guild_cmds:
            if c.get("name") in REMOVED_CMD_NAMES:
                await http.delete_guild_command(app_id, guild_id, c["id"])
                print(f"Deleted guild command /{c['name']} for guild {guild_id}")
    except Exception as e:
        print(f"Failed to purge guild({guild_id}) commands: {e}")

async def _wipe_all_global_commands(app_id: int, http):
    """以『空清單』bulk 覆寫全域斜線指令，徹底清掉殘留與快取。"""
    try:
        await http.bulk_upsert_global_commands(app_id, [])
        print("Wiped ALL global commands.")
    except Exception as e:
        print(f"Failed to wipe global commands: {e}")

@bot.event
async def on_ready():
    await init_db()
    try:
        app_id = bot.application_id

        # 0) 先清空全域指令，避免殘留與延遲
        await _wipe_all_global_commands(app_id, bot.http)

        # 1) 將程式中定義的指令複製到各 guild 並同步（guild-level 即時可用）
        for g in bot.guilds:
            guild_obj = discord.Object(id=g.id)
            #（保險）清舊指令名稱
            await _purge_removed_commands_for_guild(app_id, g.id, bot.http)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Guild {g.id} synced {len(synced)} commands: {[c.name for c in synced]}")

        # 不呼叫全域 sync，避免再次建立全域指令
    except Exception as e:
        print("Command sync/cleanup failed:", e)

    print(f"Logged in as {bot.user}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    """新加入伺服器時：把定義複製到該 guild，並同步"""
    try:
        app_id = bot.application_id
        await _purge_removed_commands_for_guild(app_id, guild.id, bot.http)
        guild_obj = discord.Object(id=guild.id)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"Joined {guild.id}, guild-synced {len(synced)} commands: {[c.name for c in synced]}")
    except Exception as e:
        print(f"on_guild_join sync failed for {guild.id}:", e)

# --------------------- 錯誤攔截與狀態 ---------------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    try:
        print("Slash command error:", repr(error))
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ 指令錯誤：{error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 指令錯誤：{error}", ephemeral=True)
    except Exception as e:
        print("Failed to send error message:", e)

STATUS_ROTATIONS = [
    "用 /owe /paid 記帳",
    "查兩人：/between",
    "撤銷：/undo",
    "看淨額：/balance",
    "看紀錄：/history",
]
_status_cycle = cycle(STATUS_ROTATIONS)

@bot.listen('on_ready')
async def _set_presence_and_start_task():
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name=next(_status_cycle))
        )
        if not _cycle_presence.is_running():
            _cycle_presence.start()
    except Exception as e:
        print("Presence setup failed:", e)

@tasks.loop(minutes=15)
async def _cycle_presence():
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name=next(_status_cycle))
        )
    except Exception as e:
        print("Presence update failed:", e)

@_cycle_presence.before_loop
async def _before_cycle_presence():
    await bot.wait_until_ready()

# --------------------- 入口 ---------------------

def main():
    if not TOKEN:
        print("請先在環境變數 DISCORD_BOT_TOKEN 設定 Bot Token")
        raise SystemExit(1)
    bot.run(TOKEN)

if __name__ == "__main__":
    asyncio.run(init_db())
    main()
