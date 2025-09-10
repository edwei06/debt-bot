# bot.py
# ------------------------------------------------------------
# Discord 多人同步記帳機器人（v2）
# 這版新增：
# 1) on_ready 會對所有已加入的 guild 立即 sync 指令（免等待全球傳播）
# 2) on_guild_join 新加入伺服器也立即 sync
# 3) 全域錯誤攔截 on_app_command_error，避免 silent fail
# ------------------------------------------------------------

import asyncio
import os
import re
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite

DB_PATH = os.getenv("LEDGER_DB", "ledger.db")
DEFAULT_CCY = os.getenv("DEFAULT_CCY", "TWD")
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.message_content = False
INTENTS.members = True

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

# --------------------- 啟動 & 同步 ---------------------

@bot.event
async def on_ready():
    await init_db()
    try:
        global_synced = await bot.tree.sync()
        print(f"Global synced {len(global_synced)} commands")
    except Exception as e:
        print("Global command sync failed:", e)
    for g in bot.guilds:
        try:
            synced = await bot.tree.sync(guild=g)
            print(f"Guild {g.id} synced {len(synced)} commands")
        except Exception as ge:
            print(f"Guild sync failed for {g.id}:", ge)
    print(f"Logged in as {bot.user}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        synced = await bot.tree.sync(guild=guild)
        print(f"Joined {guild.id}, guild-synced {len(synced)} commands")
    except Exception as e:
        print(f"on_guild_join sync failed for {guild.id}:", e)

# 全域錯誤攔截，避免 silent fail
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

# --------------------- Slash Commands ---------------------

@bot.tree.command(name="owe", description="我欠對方金額（建立債務）")
@app_commands.describe(
    user="對方（被欠錢的人）",
    amount="金額（例如 120 或 120.50）",
    note="備註"
)
async def owe(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: str,
    note: Optional[str] = None
):
    try:
        cents = await parse_amount_to_cents(amount)
        entry_id = await add_entry(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            creditor_id=user.id,
            debtor_id=interaction.user.id,
            amount_cents=cents,
            currency=DEFAULT_CCY,
            kind='debt',
            note=note,
            created_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ 已記錄：你欠 {user.mention} {cents/100:.2f} {DEFAULT_CCY}（# {entry_id}）"
            + (f"｜{note}" if note else "")
        )
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)


@bot.tree.command(name="lent", description="我借給對方（對方欠我）")
@app_commands.describe(
    user="對方（欠你的人）",
    amount="金額（例如 120 或 120.50）",
    note="備註"
)
async def lent(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: str,
    note: Optional[str] = None
):
    try:
        cents = await parse_amount_to_cents(amount)
        entry_id = await add_entry(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            creditor_id=interaction.user.id,
            debtor_id=user.id,
            amount_cents=cents,
            currency=DEFAULT_CCY,
            kind='debt',
            note=note,
            created_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ 已記錄：{user.mention} 欠你 {cents/100:.2f} {DEFAULT_CCY}（# {entry_id}）"
            + (f"｜{note}" if note else "")
        )
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)


@bot.tree.command(name="paid", description="我已支付給對方（減少債務）")
@app_commands.describe(
    user="給錢的對象",
    amount="金額（例如 120 或 120.50）",
    note="備註"
)
async def paid(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: str,
    note: Optional[str] = None
):
    try:
        cents = await parse_amount_to_cents(amount)
        entry_id = await add_entry(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            creditor_id=interaction.user.id,
            debtor_id=user.id,
            amount_cents=cents,
            currency=DEFAULT_CCY,
            kind='payment',
            note=note or 'payment',
            created_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"💸 已記錄付款：{user.mention} ← {cents/100:.2f} {DEFAULT_CCY}（# {entry_id}）"
            + (f"｜{note}" if note else "")
        )
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)


@bot.tree.command(name="balance", description="查看與某人的淨額，或列出前幾名對手方")
@app_commands.describe(user="可選，指定對象則顯示雙方淨額")
async def balance(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None
):
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
            if net > 0:
                lines.append(f"{mention} 淨欠你 {net/100:.2f} {DEFAULT_CCY}")
            else:
                lines.append(f"你淨欠 {mention} {abs(net)/100:.2f} {DEFAULT_CCY}")
        await interaction.response.send_message("📈 你的前幾名對手方：\n" + "\n".join(lines))


@bot.tree.command(name="history", description="查看最近的記錄")
@app_commands.describe(user="可選，限定與此人之間", limit="筆數，預設 10")
async def history(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    limit: Optional[int] = 10
):
    limit = max(1, min(50, limit or 10))
    rows = await recent_entries(interaction.guild_id, interaction.user.id, user.id if user else None, limit)
    if not rows:
        await interaction.response.send_message("📝 尚無記錄。", ephemeral=True)
        return

    def line(r):
        _id, cred, debt, cents, ccy, kind, note, created_by, created_at = r
        sign = "→"
        return (
            f"#{_id} [{ccy}] {cents/100:.2f} {kind} | "
            f"<@{debt}> {sign} <@{cred}> | by <@{created_by}> | {created_at}"
            + (f" ｜{note}" if note else "")
        )

    text = "\n".join(line(r) for r in rows)
    await interaction.response.send_message("🧾 最近記錄：\n" + text)


@bot.tree.command(name="split_equal", description="均分支出（由你先墊付）")
@app_commands.describe(
    total="總金額 (如 900 或 900.00)",
    participants_mentions="輸入 @提及 的清單，例如：@A @B @C（不含自己）",
    note="備註"
)
async def split_equal(
    interaction: discord.Interaction,
    total: str,
    participants_mentions: str,
    note: Optional[str] = None
):
    try:
        total_cents = await parse_amount_to_cents(total)
        ids = [int(x) for x in re.findall(r"<@!?([0-9]+)>", participants_mentions)]
        ids = [i for i in ids if i != interaction.user.id]
        unique_ids = sorted(set(ids))
        if not unique_ids:
            await interaction.response.send_message("請至少指定一位參與者（不含自己）", ephemeral=True)
            return

        share = total_cents // len(unique_ids)
        remainder = total_cents - share * len(unique_ids)

        created = []
        for idx, uid in enumerate(unique_ids):
            cents = share + (1 if idx < remainder else 0)
            entry_id = await add_entry(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                creditor_id=interaction.user.id,
                debtor_id=uid,
                amount_cents=cents,
                currency=DEFAULT_CCY,
                kind='split',
                note=note or f'split {total_cents/100:.2f} among {len(unique_ids)}',
                created_by=interaction.user.id
            )
            created.append((uid, cents, entry_id))

        lines = [f"<@{uid}> 欠你 {c/100:.2f} {DEFAULT_CCY}（# {eid}）" for uid, c, eid in created]
        await interaction.response.send_message("🍰 已建立均分：\n" + "\n".join(lines))
    except ValueError as ve:
        await interaction.response.send_message(f"❌ {ve}", ephemeral=True)


# --------------------- 入口 ---------------------

def main():
    if not TOKEN:
        print("請先在環境變數 DISCORD_BOT_TOKEN 設定 Bot Token")
        raise SystemExit(1)
    bot.run(TOKEN)

if __name__ == "__main__":
    asyncio.run(init_db())
    main()
