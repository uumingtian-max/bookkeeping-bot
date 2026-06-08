#!/usr/bin/env python3
"""Telegram 多群多币种记账机器人 - 全功能版"""
import os, sys, json, sqlite3, csv, io, re, time
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip())
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)) + "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = sqlite3.connect(f"{DATA_DIR}/bookkeeping.db", check_same_thread=False)
DB.row_factory = sqlite3.Row

# ====================== RATE ENGINE ======================
SPREAD = 0.015  # 内置上浮
RATE_CACHE = {"raw": 0, "time": 0, "ttl": 300}  # 缓存5分钟

def fetch_raw_rate():
    """从CoinGecko/Binance获取USDT/CNY实时裸价"""
    # 主: CoinGecko (免费, 无key, 稳定)
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=cny"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=10).read())
        return float(data["tether"]["cny"])
    except Exception:
        pass
    # 备: CryptoCompare
    try:
        url = "https://min-api.cryptocompare.com/data/price?fsym=USDT&tsyms=CNY"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=10).read())
        return float(data["CNY"])
    except Exception:
        pass
    # 备2: Bybit
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDTUSDC"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=10).read())
        if data["retCode"] == 0 and data["result"]["list"]:
            return float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        pass
    return None

def get_live_rate():
    """获取实时裸价 + 上浮0.015 = 展示价"""
    now = time.time()
    if now - RATE_CACHE["time"] < RATE_CACHE["ttl"] and RATE_CACHE["raw"] > 0:
        return RATE_CACHE["raw"], RATE_CACHE["raw"] + SPREAD
    
    raw = fetch_raw_rate()
    if raw:
        RATE_CACHE["raw"] = raw
        RATE_CACHE["time"] = now
        return raw, raw + SPREAD
    return None, None

RATE_MANUAL = False  # 是否手动模式

def get_rate():
    """当前有效汇率：手动优先，否则实时+上浮"""
    if RATE_MANUAL:
        row = DB.execute("SELECT usdt_to_cny FROM rate ORDER BY id DESC LIMIT 1").fetchone()
        return row["usdt_to_cny"]
    _, display = get_live_rate()
    if display:
        return display
    row = DB.execute("SELECT usdt_to_cny FROM rate ORDER BY id DESC LIMIT 1").fetchone()
    return row["usdt_to_cny"]

# ====================== DATABASE ======================
def init_db():
    DB.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE NOT NULL,
            title TEXT DEFAULT '',
            join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'CNY',
            category TEXT DEFAULT '其他',
            note TEXT DEFAULT '',
            bill_type TEXT DEFAULT 'expense',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            level INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS rate (
            id INTEGER PRIMARY KEY,
            usdt_to_cny REAL DEFAULT 7.2,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            remind_time TIMESTAMP NOT NULL,
            content TEXT NOT NULL,
            done INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_bills_chat ON bills(chat_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bills_user ON bills(user_id, created_at);
    """)
    DB.commit()
    # default rate
    if not DB.execute("SELECT COUNT(*) FROM rate").fetchone()[0]:
        DB.execute("INSERT INTO rate (usdt_to_cny) VALUES (7.2)")
        DB.commit()

init_db()

# ====================== HELPERS ======================
def is_admin(user_id):
    if user_id in ADMIN_IDS:
        return True
    row = DB.execute("SELECT level FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def get_rate():
    return DB.execute("SELECT usdt_to_cny FROM rate ORDER BY id DESC LIMIT 1").fetchone()["usdt_to_cny"]

def fmt_amount(amount, currency):
    if currency.upper() == "USDT":
        return f"{amount:+.2f} USDT (≈{amount * get_rate():.2f} CNY)"
    return f"{amount:+.2f} CNY"

def fmt_time(ts):
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") if isinstance(ts, str) else ts
    if isinstance(ts, str):
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    return dt.strftime("%m-%d %H:%M")

def parse_amount(text):
    """解析金额: '100', '+100', '-50', '100usdt', '+200cny'"""
    text = text.strip().upper()
    sign = 1
    if text.startswith("+"):
        sign = 1
        text = text[1:]
    elif text.startswith("-"):
        sign = -1
        text = text[1:]

    currency = "CNY"
    if text.endswith("USDT"):
        currency = "USDT"
        text = text[:-4].strip()

    try:
        amount = float(text) * sign
        return amount, currency
    except:
        return None, None

# ====================== COMMANDS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *多币种记账机器人*\n\n"
        "直接发消息记账：\n"
        "`100` — 支出100 CNY\n"
        "`+500` — 收入500 CNY\n"
        "`50usdt` — 支出50 USDT\n"
        "`+200usdt` — 收入200 USDT\n"
        "`100 餐饮` — 带分类\n"
        "`100 晚餐很贵` — 带备注\n"
        "`100 #餐饮 备注` — 分类+备注\n\n"
        "📋 命令（中英文皆可）：\n"
        "/今日 /昨天 /本周 /本月 — 统计\n"
        "/账单 — 最近20条\n"
        "/排行 — 分类排行\n"
        "/删 ID — 删除记录\n"
        "/汇率 — 实时汇率(裸价+上浮0.015)\n"
        "/汇率更 — 刷新 | /汇率设 7.3 — 手动\n"
        "/群发 内容 — 群发所有群\n"
        "/memo 内容 — 备忘 | /备忘录 — 查看\n"
        "/提醒 30 内容 — 定时提醒\n"
        "/导出 — 导出CSV\n"
        "/群组 — 查看所在群\n"
        "/帮助 — 本消息\n\n"
        "英文快捷: /today /month /rank /rate 等",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ---- Recording ----
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name or str(user_id)

    # Ensure group registered
    DB.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)",
               (chat_id, update.effective_chat.title or f"Chat{chat_id}"))
    DB.commit()

    # Parse: amount [category] [note], currency suffix
    parts = text.split(None, 1)
    first = parts[0]

    amount, currency = parse_amount(first)
    if amount is None:
        return  # not a number, ignore silently or could be a memo

    category = "其他"
    note = ""
    if len(parts) > 1:
        rest = parts[1]
        # check for #category
        m = re.match(r'#(\S+)\s*(.*)', rest)
        if m:
            category = m.group(1)
            note = m.group(2)
        else:
            note = rest

    bill_type = "income" if amount > 0 else "expense"
    amount_abs = abs(amount)

    DB.execute(
        "INSERT INTO bills (chat_id, user_id, username, amount, currency, category, note, bill_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, username, amount_abs, currency, category, note, bill_type)
    )
    DB.commit()

    # Response
    emoji = "💰" if bill_type == "income" else "💸"
    sign = "+" if bill_type == "income" else "-"
    reply = f"{emoji} {sign}{amount_abs:.2f} {currency}  [{category}]"
    if note:
        reply += f" — {note}"
    reply += f"\n👤 {username}"
    await update.message.reply_text(reply)

# ---- Statistics ----
async def stats_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stats(update, "今日", days=0)
async def stats_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stats(update, "昨日", days=1)
async def stats_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stats(update, "本周", days=7)
async def stats_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stats(update, "本月", days=30)

async def send_stats(update: Update, label, days):
    chat_id = update.effective_chat.id
    now = datetime.now()
    if days == 0:
        start = now.strftime("%Y-%m-%d")
    else:
        start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = DB.execute(
        "SELECT bill_type, currency, SUM(amount) as total, COUNT(*) as cnt "
        "FROM bills WHERE chat_id=? AND date(created_at) >= ? "
        "GROUP BY bill_type, currency",
        (chat_id, start)
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"📭 {label}暂无记录")
        return

    msg = f"📊 *{label}统计*\n"
    by_currency = defaultdict(lambda: {"income": 0, "expense": 0})
    for r in rows:
        by_currency[r["currency"]][r["bill_type"]] += r["total"]

    for cur, data in sorted(by_currency.items()):
        inc = data["income"]
        exp = data["expense"]
        net = inc - exp
        msg += f"\n💎 *{cur}*: 收 {inc:+.2f} | 支 {exp:+.2f} | 净 {net:+.2f}"
        if cur.upper() == "USDT":
            msg += f" (≈{net * get_rate():.2f} CNY)"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Bill list ----
async def bill_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = DB.execute(
        "SELECT * FROM bills WHERE chat_id=? ORDER BY created_at DESC LIMIT 20",
        (chat_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📭 暂无账单")
        return

    msg = "📋 *最近20条账单*\n\n"
    for r in rows:
        emoji = "💰" if r["bill_type"] == "income" else "💸"
        sign = "+" if r["bill_type"] == "income" else "-"
        msg += f"`{r['id']}` {emoji} {sign}{r['amount']:.2f} {r['currency']} [{r['category']}]"
        if r["note"]:
            msg += f" _{r['note']}_"
        msg += f" — {fmt_time(r['created_at'])}\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Ranking ----
async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = DB.execute(
        "SELECT category, currency, SUM(amount) as total, COUNT(*) as cnt "
        "FROM bills WHERE chat_id=? AND bill_type='expense' "
        "GROUP BY category, currency ORDER BY total DESC LIMIT 15",
        (chat_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📭 暂无数据")
        return

    msg = "🏆 *支出分类排行*\n\n"
    for i, r in enumerate(rows, 1):
        medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        msg += f"{medal} {r['category']}: {r['total']:.2f} {r['currency']} ({r['cnt']}笔)\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Delete ----
async def delete_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /删 账单ID")
        return
    try:
        bill_id = int(context.args[0])
    except:
        await update.message.reply_text("ID必须是数字")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    row = DB.execute("SELECT * FROM bills WHERE id=? AND chat_id=?", (bill_id, chat_id)).fetchone()
    if not row:
        await update.message.reply_text("❌ 账单不存在")
        return

    if row["user_id"] != user_id and not is_admin(user_id):
        await update.message.reply_text("❌ 只能删除自己的账单")
        return

    DB.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    DB.commit()
    await update.message.reply_text(f"✅ 已删除 #{bill_id}: {row['amount']} {row['currency']} [{row['category']}]")

# ---- Exchange Rate ----
async def show_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "🔧手动" if RATE_MANUAL else "🤖自动"
    raw, display = get_live_rate()
    current = get_rate()
    msg = f"💱 *实时汇率* [{mode}]\n"
    if raw:
        age = int(time.time() - RATE_CACHE["time"])
        msg += f"裸价: 1 USDT = {raw:.4f} CNY\n"
        msg += f"报价: 1 USDT = *{current:.4f} CNY*\n"
        msg += f"上浮 +{SPREAD} | 缓存 {age}s"
    else:
        msg += f"1 USDT = *{current:.4f} CNY*\n⚠️ 实时获取失败"
    if is_admin(update.effective_user.id):
        msg += "\n\n🔧 `/汇率更` 自动 | `/汇率设 7.3` 手动"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新汇率", callback_data="refresh_rate")]
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def refresh_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """强制刷新汇率 → 切回自动模式"""
    global RATE_MANUAL
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员")
        return
    RATE_MANUAL = False
    RATE_CACHE["time"] = 0
    raw, display = get_live_rate()
    if raw:
        await update.message.reply_text(f"🔄 已切回自动模式\n裸价: {raw:.4f} | 报价: {display:.4f} (+{SPREAD})")
    else:
        await update.message.reply_text("❌ 刷新失败，稍后重试")

async def rate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RATE_MANUAL
    query = update.callback_query
    await query.answer()
    RATE_MANUAL = False
    RATE_CACHE["time"] = 0
    raw, display = get_live_rate()
    current = get_rate()
    if raw:
        await query.edit_message_text(
            f"💱 *实时汇率* [🤖自动]\n裸价: 1 USDT = {raw:.4f} CNY\n报价: 1 USDT = *{current:.4f} CNY*\n+{SPREAD} | 刚刷新\n\n🔧 `/汇率设 7.3` 手动",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新汇率", callback_data="refresh_rate")]
            ])
        )
    else:
        await query.edit_message_text("❌ 刷新失败")

async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RATE_MANUAL
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员可用")
        return
    if not context.args:
        await update.message.reply_text("用法: /汇率设7.3")
        return
    try:
        new_rate = float(context.args[0])
    except:
        await update.message.reply_text("请输入有效数字")
        return
    DB.execute("INSERT INTO rate (usdt_to_cny) VALUES (?)", (new_rate,))
    DB.commit()
    RATE_MANUAL = True  # 切手动模式
    RATE_CACHE["time"] = 0
    await update.message.reply_text(f"✅ 手动模式: 1 USDT = {new_rate} CNY\n用 `/汇率更` 切回自动")

# ---- Broadcast ----
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员可用")
        return
    if not context.args:
        await update.message.reply_text("用法: /群发 消息内容")
        return

    text = " ".join(context.args)
    groups = DB.execute("SELECT chat_id FROM groups").fetchall()
    success = 0
    fail = 0
    for g in groups:
        try:
            await context.bot.send_message(chat_id=g["chat_id"], text=f"📢 *群发通知*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            success += 1
        except:
            fail += 1

    await update.message.reply_text(f"✅ 群发完成: 成功 {success} 群, 失败 {fail} 群")

# ---- Memo ----
async def memo_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /memo 内容")
        return
    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    DB.execute("INSERT INTO memos (chat_id, user_id, content) VALUES (?, ?, ?)", (chat_id, user_id, text))
    DB.commit()
    await update.message.reply_text(f"📝 已备忘!")

async def memo_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = DB.execute("SELECT * FROM memos WHERE chat_id=? ORDER BY created_at DESC LIMIT 10", (chat_id,)).fetchall()
    if not rows:
        await update.message.reply_text("📭 暂无备忘")
        return
    msg = "📝 *备忘录*\n\n"
    for r in rows:
        msg += f"`{r['id']}` {r['content']} — _{fmt_time(r['created_at'])}_\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Export ----
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = DB.execute(
        "SELECT id, username, amount, currency, category, note, bill_type, created_at "
        "FROM bills WHERE chat_id=? ORDER BY created_at DESC",
        (chat_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📭 无数据可导出")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "用户", "金额", "币种", "分类", "备注", "类型", "时间"])
    for r in rows:
        writer.writerow([r["id"], r["username"], r["amount"], r["currency"], r["category"],
                         r["note"], r["bill_type"], r["created_at"]])

    output.seek(0)
    buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    buf.name = f"bills_{datetime.now().strftime('%Y%m%d')}.csv"
    await update.message.reply_document(document=buf, filename=buf.name,
                                        caption="📎 账单导出")

# ---- Admin ----
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) and update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ 仅超级管理员可用")
        return
    if not context.args:
        await update.message.reply_text("用法: /加管理 @username 或回复某人消息")
        return

    target = context.args[0].replace("@", "")
    # Try by username
    await update.message.reply_text("请直接回复某人的消息然后用 /加管理 来添加")

async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员可用")
        return
    rows = DB.execute("SELECT * FROM groups ORDER BY join_time DESC").fetchall()
    msg = "📋 *机器人所在群组*\n\n"
    for r in rows:
        msg += f"• `{r['chat_id']}` — {r['title']}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Reminder ----
async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("用法: /提醒 30 内容 (30分钟后提醒)\n/提醒 18:00 内容\n/提醒 2026-06-10 内容")
        return

    time_str = context.args[0]
    content = " ".join(context.args[1:])
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    now = datetime.now()
    # Parse time
    if ":" in time_str and "-" not in time_str:
        # HH:MM format
        h, m = time_str.split(":")
        remind_time = now.replace(hour=int(h), minute=int(m), second=0)
        if remind_time < now:
            remind_time += timedelta(days=1)
    elif time_str.isdigit():
        # minutes from now
        remind_time = now + timedelta(minutes=int(time_str))
    elif "-" in time_str:
        remind_time = datetime.strptime(time_str[:16], "%Y-%m-%d %H:%M")
    else:
        await update.message.reply_text("时间格式错误")
        return

    DB.execute("INSERT INTO reminders (chat_id, user_id, remind_time, content) VALUES (?, ?, ?, ?)",
               (chat_id, user_id, remind_time, content))
    DB.commit()
    await update.message.reply_text(f"⏰ 已设置提醒: {remind_time.strftime('%m-%d %H:%M')} — {content}")

# ====================== MAIN ======================
# 中文别名路由表
CHINESE_ALIASES = {
    "今日": stats_today, "昨天": stats_yesterday, "本周": stats_week, "本月": stats_month,
    "账单": bill_list, "排行": ranking, "删": delete_bill,
    "汇率": show_rate, "汇率更": refresh_rate, "汇率设": set_rate,
    "群发": broadcast, "备忘录": memo_list, "导出": export_csv,
    "群组": list_groups, "加管理": add_admin, "提醒": remind,
    "帮助": help_cmd,
}

async def route_chinese(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """路由 /中文命令 到对应处理器"""
    text = update.message.text.strip()
    if not text.startswith("/"):
        await handle_message(update, context)
        return
    cmd = text[1:].split()[0].split("@")[0]  # 去掉 / 和 @botname
    # 重建 context.args
    parts = text.split()
    context.args = parts[1:] if len(parts) > 1 else []
    if cmd in CHINESE_ALIASES:
        await CHINESE_ALIASES[cmd](update, context)
    else:
        await handle_message(update, context)  # 可能是 /100 这样的记账

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN environment variable is required!")
        sys.exit(1)

    app = Application.builder().token(TOKEN).build()

    # 拉丁命令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", stats_today))
    app.add_handler(CommandHandler("yesterday", stats_yesterday))
    app.add_handler(CommandHandler("week", stats_week))
    app.add_handler(CommandHandler("month", stats_month))
    app.add_handler(CommandHandler("list", bill_list))
    app.add_handler(CommandHandler("rank", ranking))
    app.add_handler(CommandHandler("del", delete_bill))
    app.add_handler(CommandHandler("rate", show_rate))
    app.add_handler(CommandHandler("rater", refresh_rate))
    app.add_handler(CommandHandler("rateset", set_rate))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("memo", memo_add))
    app.add_handler(CommandHandler("memos", memo_list))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CommandHandler("groups", list_groups))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CallbackQueryHandler(rate_callback, pattern="refresh_rate"))
    # 中文别名 + 记账消息 (全部走这个兜底)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_chinese))

    print("🤖 记账机器人启动中...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
