#!/usr/bin/env python3
"""Telegram 超级记账机器人 - AI驱动多群多币种全功能版"""
import os, sys, json, sqlite3, csv, io, re, time, threading
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

TOKEN = os.environ.get("BOT_TOKEN", "8638712861:AAFaWkeCAfQp07cCrUi3YX8QFWMGf-7_gGM")
ADMIN_IDS = set(int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip())
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)) + "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = sqlite3.connect(f"{DATA_DIR}/bookkeeping.db", check_same_thread=False)
DB.row_factory = sqlite3.Row

# ====================== RATE ENGINE ======================
SPREAD = 0.015
RATE_CACHE = {"raw": 0, "time": 0, "ttl": 300}
RATE_MANUAL = False

def fetch_raw_rate():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=cny"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=10).read())
        return float(data["tether"]["cny"])
    except: pass
    try:
        url = "https://min-api.cryptocompare.com/data/price?fsym=USDT&tsyms=CNY"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=10).read())
        return float(data["CNY"])
    except: pass
    return None

def get_live_rate():
    now = time.time()
    if now - RATE_CACHE["time"] < RATE_CACHE["ttl"] and RATE_CACHE["raw"] > 0:
        return RATE_CACHE["raw"], RATE_CACHE["raw"] + SPREAD
    raw = fetch_raw_rate()
    if raw:
        RATE_CACHE["raw"], RATE_CACHE["time"] = raw, now
        return raw, raw + SPREAD
    return None, None

def get_rate():
    if RATE_MANUAL:
        row = DB.execute("SELECT usdt_to_cny FROM rate ORDER BY id DESC LIMIT 1").fetchone()
        return row["usdt_to_cny"]
    _, display = get_live_rate()
    if display: return display
    row = DB.execute("SELECT usdt_to_cny FROM rate ORDER BY id DESC LIMIT 1").fetchone()
    return row["usdt_to_cny"]

def parse_amount(text):
    """解析金额: +1000 = 充值1000CNY, 下发500usdt = -500USDT, 50 = +50CNY"""
    text = text.strip()
    is_deposit = True
    currency = "CNY"
    # 充值/下发关键词
    for kw in ["下发", "下分", "提现", "支出", "转出", "付款", "支付"]:
        if kw in text:
            is_deposit = False; text = text.replace(kw, ""); break
    for kw in ["充值", "上分", "收入", "入金", "转入", "收款"]:
        if kw in text:
            is_deposit = True; text = text.replace(kw, ""); break
    # +/- 符号
    if text.startswith("+") or text.startswith("＋"):
        is_deposit = True; text = text[1:]
    elif text.startswith("-") or text.startswith("－"):
        is_deposit = False; text = text[1:]
    # 货币
    for kw in ["usdt", "USDT", "u", "U"]:
        if kw in text.split() or text.lower().endswith(kw):
            currency = "USDT"; text = re.sub(r'(?i)usdt|u$', '', text).strip(); break
    # 提取金额
    m = re.search(r'[\d,.]+', text)
    if not m: return None, None, None
    amount = float(m.group().replace(",", ""))
    # 备注和分类
    after = text[m.end():].strip()
    category = ""; note = ""
    m_cat = re.search(r'#(\S+)', after)
    if m_cat:
        category = m_cat.group(1)
        after = after[:m_cat.start()] + " " + after[m_cat.end():]
    note = after.strip()
    return amount, currency, is_deposit, category, note

# ====================== DATABASE ======================
def init_db():
    DB.execute('''CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, user_id INTEGER, username TEXT,
        amount REAL, currency TEXT DEFAULT 'CNY',
        type TEXT DEFAULT 'deposit', -- deposit/withdrawal
        category TEXT DEFAULT '', note TEXT DEFAULT '',
        usdt_to_cny REAL DEFAULT 7.2,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS rate (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usdt_to_cny REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS memos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, user_id INTEGER, content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, user_id INTEGER, remind_at TEXT,
        content TEXT, done INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, category TEXT, currency TEXT,
        amount REAL, period TEXT DEFAULT 'monthly' -- daily/weekly/monthly
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS debts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, from_user TEXT, to_user TEXT,
        amount REAL, currency TEXT DEFAULT 'CNY',
        note TEXT, status TEXT DEFAULT 'open', -- open/closed
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    DB.execute('''CREATE TABLE IF NOT EXISTS recurring (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT, user_id INTEGER,
        amount REAL, currency TEXT DEFAULT 'CNY',
        type TEXT DEFAULT 'deposit',
        category TEXT, note TEXT,
        cron TEXT, -- daily/weekly/monthly
        next_run TEXT, active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    row = DB.execute("SELECT COUNT(*) FROM rate").fetchone()
    if row[0] == 0:
        DB.execute("INSERT INTO rate (usdt_to_cny) VALUES (7.2)")
    DB.commit()

init_db()

# ====================== HELPERS ======================
def is_admin(uid): return uid in ADMIN_IDS

def fmt_amount(amount, currency):
    return f"{amount:,.2f} {currency}"

def get_group_stats(group_id, period="today"):
    """返回 (deposit_count, deposit_total, deposit_usdt, withdraw_count, withdraw_total, withdraw_usdt)"""
    now = datetime.now()
    if period == "today":
        start = now.strftime("%Y-%m-%d 00:00:00")
    elif period == "yesterday":
        start = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d 00:00:00")
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d 00:00:00")
    elif period == "month":
        start = now.strftime("%Y-%m-01 00:00:00")
    else:
        start = period
    
    params = [group_id, start]
    where = "group_id=? AND created_at>=? "
    if period == "yesterday":
        where += "AND created_at<?"
        params.append(end)
    params = tuple(params)
    
    rate = get_rate()
    dep = DB.execute(f"SELECT COUNT(*), SUM(amount) FROM records WHERE {where} AND type='deposit' AND currency='CNY'", params).fetchone()
    dep_u = DB.execute(f"SELECT COUNT(*), SUM(amount) FROM records WHERE {where} AND type='deposit' AND currency='USDT'", params).fetchone()
    wit = DB.execute(f"SELECT COUNT(*), SUM(amount) FROM records WHERE {where} AND type='withdrawal' AND currency='CNY'", params).fetchone()
    wit_u = DB.execute(f"SELECT COUNT(*), SUM(amount) FROM records WHERE {where} AND type='withdrawal' AND currency='USDT'", params).fetchone()
    
    dep_cnt = (dep[0] or 0) + (dep_u[0] or 0)
    dep_cny = (dep[1] or 0) + (dep_u[1] or 0) * rate
    dep_usdt = (dep_u[1] or 0) + (dep[1] or 0) / rate
    wit_cnt = (wit[0] or 0) + (wit_u[0] or 0)
    wit_cny = (wit[1] or 0) + (wit_u[1] or 0) * rate
    wit_usdt = (wit_u[1] or 0) + (wit[1] or 0) / rate
    return dep_cnt, dep_cny, dep_usdt, wit_cnt, wit_cny, wit_usdt

def stats_message(group_id, title, period="today"):
    dep_cnt, dep_cny, dep_usdt, wit_cnt, wit_cny, wit_usdt = get_group_stats(group_id, period)
    rate = get_rate()
    balance = dep_cny - wit_cny
    msg = f"*{title}* [{period}]\n"
    msg += f"📥 充值: *{dep_cnt}* 笔 / *{dep_cny:,.2f}* CNY"
    if dep_usdt > 0: msg += f" ({dep_usdt:,.2f} USDT)"
    msg += f"\n📤 下发: *{wit_cnt}* 笔 / *{wit_cny:,.2f}* CNY"
    if wit_usdt > 0: msg += f" ({wit_usdt:,.2f} USDT)"
    msg += f"\n💰 余额: *{balance:,.2f}* CNY / *{balance/rate:,.2f}* USDT"
    msg += f"\n💱 汇率: {rate:.4f}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📟 最新10笔", callback_data=f"recent_{group_id}_10"),
         InlineKeyboardButton("🔄 刷新", callback_data=f"refresh_{group_id}_{period}")],
        [InlineKeyboardButton("📊 全局统计", callback_data="global_stats"),
         InlineKeyboardButton("📈 趋势图", callback_data=f"chart_{group_id}")],
    ])
    return msg, kb

def global_stats_message():
    row = DB.execute("SELECT COUNT(DISTINCT group_id) FROM records").fetchone()
    group_count = row[0] or 0
    rate = get_rate()
    
    dep = DB.execute("SELECT COUNT(*), SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) FROM records WHERE type='deposit'", (rate,)).fetchone()
    wit = DB.execute("SELECT COUNT(*), SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) FROM records WHERE type='withdrawal'", (rate,)).fetchone()
    dep_cnt, dep_cny = dep[0] or 0, dep[1] or 0
    wit_cnt, wit_cny = wit[0] or 0, wit[1] or 0
    
    msg = f"📊 *全局统计*\n"
    msg += f"👥 群组: *{group_count}* 个\n"
    msg += f"📥 累计充值: *{dep_cnt}* 笔 / *{dep_cny:,.2f}* CNY ({dep_cny/rate:,.2f} USDT)\n"
    msg += f"📤 累计下发: *{wit_cnt}* 笔 / *{wit_cny:,.2f}* CNY ({wit_cny/rate:,.2f} USDT)\n"
    msg += f"💰 总余额: *{dep_cny - wit_cny:,.2f}* CNY\n"
    msg += f"💱 汇率: {rate:.4f}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="global_refresh")]])
    return msg, kb

def text_chart(group_id):
    """文字柱状图：分类排行"""
    rate = get_rate()
    rows = DB.execute("""
        SELECT category, SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) as total, COUNT(*) as cnt
        FROM records WHERE group_id=? AND type='withdrawal' AND category!=''
        GROUP BY category ORDER BY total DESC LIMIT 10
    """, (rate, group_id)).fetchall()
    if not rows: return "📊 暂无数据"
    max_len = 15
    max_total = rows[0][1]
    msg = "📊 *支出分类排行*\n```\n"
    for cat, total, cnt in rows:
        bar_len = int(total / max_total * max_len) if max_total > 0 else 0
        bar = "█" * bar_len + "░" * (max_len - bar_len)
        msg += f"{cat:<8} {bar} {total:>10,.2f}\n"
    msg += "```"
    return msg

# ====================== COMMANDS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 今日统计", callback_data=f"stats_today"),
         InlineKeyboardButton("📅 本月统计", callback_data=f"stats_month")],
        [InlineKeyboardButton("💱 查汇率", callback_data="refresh_rate"),
         InlineKeyboardButton("📟 最新10笔", callback_data=f"recent_all_10")],
        [InlineKeyboardButton("📊 全局统计", callback_data="global_stats"),
         InlineKeyboardButton("❓ 帮助", callback_data="help_full")],
    ])
    await update.message.reply_text(
        f"🤖 *超级记账机器人* v2.0\n"
        f"多群 · 多币种 · USDT/CNY · 汇率上浮\n"
        f"充值/下发 · 预算预警 · 债务追踪\n\n"
        f"📝 直接输入金额记账：\n"
        f"`+1000` 充值CNY | `下发500usdt` 下分\n"
        f"`100 #餐饮 外卖` 支出\n\n"
        f"⚡ 命令: /账单 /排行 /全局 /图表\n"
        f"🔧 /预算 /债务 /搜索 /复刻 /备份",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def stats_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    msg, kb = stats_message(gid, "📊 今日统计", "today")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def stats_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    msg, kb = stats_message(gid, "📅 本周统计", "week")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def stats_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    msg, kb = stats_message(gid, "📅 本月统计", "month")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def show_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, kb = global_stats_message()
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def view_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    await update.message.reply_text(text_chart(gid), parse_mode=ParseMode.MARKDOWN)

async def bill_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    limit = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    rows = DB.execute("SELECT * FROM records WHERE group_id=? ORDER BY id DESC LIMIT ?", (gid, limit)).fetchall()
    if not rows:
        await update.message.reply_text("📭 暂无账单")
        return
    rate = get_rate()
    msg = f"📟 *最新{len(rows)}笔*\n\n"
    for r in rows:
        emoji = "📥" if r["type"] == "deposit" else "📤"
        amt = r["amount"]
        if r["currency"] == "USDT":
            amt_display = f"{amt} USDT (≈{amt*rate:,.2f} CNY)"
        else:
            amt_display = f"{amt} CNY"
        cat = f" #{r['category']}" if r["category"] else ""
        note = f" {r['note']}" if r["note"] else ""
        user = r["username"] or str(r["user_id"])
        msg += f"`{r['id']}` {emoji} *{amt_display}*{cat}{note} — {user}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def search_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("用法: `/搜索 关键词 或 #分类 或 >100`\n示例: `/搜索 #餐饮` `/搜索 外卖` `/搜索 >500`")
        return
    query = " ".join(context.args)
    where = "group_id=? "; params = [gid]
    if query.startswith("#"):
        where += "AND category=? "; params.append(query[1:])
    elif query.startswith(">"):
        try:
            amt = float(query[1:])
            where += "AND amount>? "; params.append(amt)
        except: pass
    elif query.startswith("<"):
        try:
            amt = float(query[1:])
            where += "AND amount<? "; params.append(amt)
        except: pass
    else:
        where += "AND (note LIKE ? OR category LIKE ? OR username LIKE ?) "
        params.extend([f"%{query}%"] * 3)
    rows = DB.execute(f"SELECT * FROM records WHERE {where}ORDER BY id DESC LIMIT 30", params).fetchall()
    if not rows:
        await update.message.reply_text("🔍 未找到匹配记录")
        return
    rate = get_rate()
    total_cny = 0
    msg = f"🔍 *搜索结果: {len(rows)}条*\n\n"
    for r in rows:
        emoji = "📥" if r["type"] == "deposit" else "📤"
        amt = r["amount"]; amt_cny = amt if r["currency"] == "CNY" else amt * rate
        total_cny += amt_cny
        amt_display = f"{amt} {r['currency']}" if r["currency"] == "CNY" else f"{amt} USDT (≈{amt*rate:,.0f} CNY)"
        cat = f" #{r['category']}" if r["category"] else ""
        msg += f"`{r['id']}` {emoji} {amt_display}{cat} {r['note']}\n"
    msg += f"\n💰 合计: {total_cny:,.2f} CNY"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    rate = get_rate()
    rows = DB.execute("""
        SELECT category, SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) as total, COUNT(*) as cnt
        FROM records WHERE group_id=? AND type='withdrawal' AND category!=''
        GROUP BY category ORDER BY total DESC LIMIT 15
    """, (rate, gid)).fetchall()
    if not rows:
        await update.message.reply_text("📭 暂无分类数据")
        return
    msg = "🏆 *分类支出排行榜*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (cat, total, cnt) in enumerate(rows):
        medal = medals[i] if i < 3 else f"  {i+1}."
        msg += f"{medal} *{cat}*: {total:,.2f} CNY ({cnt}笔)\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 图表视图", callback_data=f"chart_{gid}")],
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def delete_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: `/删除 ID` 或 `/删除 ID1 ID2 ID3`")
        return
    gid = str(update.effective_chat.id)
    uid = update.effective_user.id
    deleted = []
    for arg in context.args:
        if not arg.isdigit(): continue
        rid = int(arg)
        row = DB.execute("SELECT * FROM records WHERE id=? AND group_id=?", (rid, gid)).fetchone()
        if not row: continue
        if not is_admin(uid) and row["user_id"] != uid:
            await update.message.reply_text(f"❌ ID {rid} 不是你的记录")
            continue
        DB.execute("DELETE FROM records WHERE id=?", (rid,))
        deleted.append(str(rid))
    DB.commit()
    if deleted:
        await update.message.reply_text(f"🗑 已删除: ID {', '.join(deleted)}")
    else:
        await update.message.reply_text("❌ 没有可删除的记录")

# ---- Exchange Rate ----
async def show_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw, display = get_live_rate()
    current = get_rate()
    age = int(time.time() - RATE_CACHE["time"]) if raw else 0
    admin = is_admin(update.effective_user.id)
    
    if admin:
        # 管理员能看到裸价和上浮
        mode = "🔧手动" if RATE_MANUAL else "🤖自动"
        msg = f"💱 *实时汇率* [{mode}]\n"
        if raw:
            msg += f"裸价: 1 USDT = {raw:.4f} CNY\n"
            msg += f"报价: 1 USDT = *{current:.4f} CNY*\n"
            msg += f"上浮 +{SPREAD} | 缓存 {age}s\n\n"
            msg += "🔧 `/汇率更` 自动 | `/汇率设 7.3` 手动"
        else:
            msg += f"1 USDT = *{current:.4f} CNY*\n⚠️ 实时获取失败"
    else:
        # 普通用户只看到最终报价
        msg = f"💱 1 USDT = *{current:.4f}* CNY"
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新汇率", callback_data="refresh_rate")]])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def refresh_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RATE_MANUAL
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员"); return
    RATE_MANUAL = False; RATE_CACHE["time"] = 0
    raw, display = get_live_rate()
    if raw:
        await update.message.reply_text(f"🔄 已切回自动\n裸价: {raw:.4f} | 报价: {display:.4f} (+{SPREAD})")
    else:
        await update.message.reply_text("❌ 刷新失败")

async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RATE_MANUAL
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员"); return
    if not context.args:
        await update.message.reply_text("用法: /汇率设7.3"); return
    try: new_rate = float(context.args[0])
    except: await update.message.reply_text("请输入有效数字"); return
    DB.execute("INSERT INTO rate (usdt_to_cny) VALUES (?)", (new_rate,)); DB.commit()
    RATE_MANUAL = True; RATE_CACHE["time"] = 0
    await update.message.reply_text(f"✅ 手动模式: 1 USDT = {new_rate} CNY\n用 `/汇率更` 切回自动")

# ---- Broadcast ----
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员"); return
    if not context.args:
        await update.message.reply_text("用法: /群发 消息内容"); return
    content = " ".join(context.args)
    groups = set(r["group_id"] for r in DB.execute("SELECT DISTINCT group_id FROM records").fetchall())
    success = 0
    for gid in groups:
        try:
            await context.bot.send_message(int(gid), f"📢 *群发通知*\n\n{content}", parse_mode=ParseMode.MARKDOWN)
            success += 1
        except: pass
    await update.message.reply_text(f"📢 已发送 {success}/{len(groups)} 个群")

# ---- Memo ----
async def memo_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /备忘 内容"); return
    gid = str(update.effective_chat.id); uid = update.effective_user.id
    content = " ".join(context.args)
    DB.execute("INSERT INTO memos (group_id, user_id, content) VALUES (?,?,?)", (gid, uid, content)); DB.commit()
    await update.message.reply_text(f"📝 已备忘: {content}")

async def memo_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    rows = DB.execute("SELECT * FROM memos WHERE group_id=? ORDER BY id DESC LIMIT 20", (gid,)).fetchall()
    if not rows:
        await update.message.reply_text("📭 暂无备忘"); return
    msg = "📝 *备忘录*\n\n"
    for r in rows:
        msg += f"`{r['id']}` {r['content']}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Budget ----
async def budget_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员"); return
    # /预算设 餐饮 5000 monthly
    if not context.args or len(context.args) < 3:
        await update.message.reply_text("用法: `/预算设 分类 金额 周期`\n周期: daily/weekly/monthly\n示例: `/预算设 餐饮 5000 monthly`")
        return
    gid = str(update.effective_chat.id)
    cat = context.args[0]
    try: amt = float(context.args[1])
    except: await update.message.reply_text("金额无效"); return
    period = context.args[2] if len(context.args) > 2 else "monthly"
    if period not in ("daily", "weekly", "monthly"):
        await update.message.reply_text("周期: daily/weekly/monthly"); return
    DB.execute("INSERT INTO budgets (group_id, category, amount, period, currency) VALUES (?,?,?,?,'CNY')",
               (gid, cat, amt, period)); DB.commit()
    await update.message.reply_text(f"✅ 预算: {cat} {amt:,.0f}CNY/{period}")

async def budget_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    budgets = DB.execute("SELECT * FROM budgets WHERE group_id=?", (gid,)).fetchall()
    if not budgets:
        await update.message.reply_text("📭 暂无预算设置"); return
    rate = get_rate()
    now = datetime.now()
    msg = "⚠️ *预算检查*\n\n"
    for b in budgets:
        if b["period"] == "daily":
            start = now.strftime("%Y-%m-%d 00:00:00")
        elif b["period"] == "weekly":
            start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d 00:00:00")
        else:
            start = now.strftime("%Y-%m-01 00:00:00")
        row = DB.execute("SELECT SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) FROM records WHERE group_id=? AND type='withdrawal' AND category=? AND created_at>=?",
                         (rate, gid, b["category"], start)).fetchone()
        spent = row[0] or 0
        pct = spent / b["amount"] * 100 if b["amount"] > 0 else 0
        bar = "▓" * int(pct/10) + "░" * (10 - int(pct/10))
        alert = " 🚨超支!" if spent > b["amount"] else ""
        msg += f"*{b['category']}* [{b['period']}]\n"
        msg += f"  {bar} {pct:.0f}%\n"
        msg += f"  {spent:,.2f} / {b['amount']:,.2f} CNY{alert}\n\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Debt ----
async def debt_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /债务 @某人 500 CNY 备注
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("用法: `/债务 @对方 金额 [CNY/USDT] [备注]`"); return
    gid = str(update.effective_chat.id)
    from_user = update.effective_user.username or str(update.effective_user.id)
    to_user = context.args[0].replace("@", "")
    try: amt = float(context.args[1])
    except: await update.message.reply_text("金额无效"); return
    currency = "CNY"
    note = ""
    if len(context.args) > 2:
        if context.args[2].upper() in ("CNY", "USDT"):
            currency = context.args[2].upper()
            note = " ".join(context.args[3:]) if len(context.args) > 3 else ""
        else:
            note = " ".join(context.args[2:])
    DB.execute("INSERT INTO debts (group_id, from_user, to_user, amount, currency, note) VALUES (?,?,?,?,?,?)",
               (gid, from_user, to_user, amt, currency, note)); DB.commit()
    await update.message.reply_text(f"🏦 已记录: @{from_user} → @{to_user} {amt} {currency} {note}")

async def debt_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    uid = update.effective_user.username or str(update.effective_user.id)
    # 我欠别人的
    owed = DB.execute("SELECT * FROM debts WHERE group_id=? AND from_user=? AND status='open'", (gid, uid)).fetchall()
    # 别人欠我的
    owed_to = DB.execute("SELECT * FROM debts WHERE group_id=? AND to_user=? AND status='open'", (gid, uid)).fetchall()
    msg = "🏦 *债务清单*\n\n"
    if owed:
        msg += "📤 *我欠别人:*\n"
        for d in owed:
            msg += f"  @{d['to_user']}: {d['amount']} {d['currency']} {d['note']}\n"
    if owed_to:
        msg += "📥 *别人欠我:*\n"
        for d in owed_to:
            msg += f"  @{d['from_user']}: {d['amount']} {d['currency']} {d['note']}\n"
    if not owed and not owed_to:
        msg += "✅ 无未清债务"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 清债", callback_data=f"debt_clear_{gid}")],
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def debt_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = query.data.split("_")[2]
    uid = query.from_user.username or str(query.from_user.id)
    DB.execute("UPDATE debts SET status='closed' WHERE group_id=? AND (from_user=? OR to_user=?) AND status='open'",
               (gid, uid, uid)); DB.commit()
    await query.edit_message_text("✅ 债务已清")

# ---- Recurring ----
async def recurring_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /复刻 每日 充值1000usdt 备注
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("用法: `/复刻 周期 金额`\n周期: 每日/每周/每月\n示例: `/复刻 每日 充值1000usdt #工资`")
        return
    gid, uid = str(update.effective_chat.id), update.effective_user.id
    period = context.args[0]
    cron_map = {"每日": "daily", "每周": "weekly", "每月": "monthly"}
    if period not in cron_map:
        await update.message.reply_text("周期: 每日/每周/每月"); return
    text = " ".join(context.args[1:])
    result = parse_amount(text)
    if result[0] is None:
        await update.message.reply_text("金额格式错误"); return
    amount, currency, is_deposit, category, note = result
    now = datetime.now()
    if cron_map[period] == "daily":
        next_run = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif cron_map[period] == "weekly":
        next_run = (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        next_month = now.month + 1; next_year = now.year
        if next_month > 12: next_month = 1; next_year += 1
        next_run = f"{next_year}-{next_month:02d}-{now.day:02d} {now.strftime('%H:%M:%S')}"
    DB.execute("INSERT INTO recurring (group_id, user_id, amount, currency, type, category, note, cron, next_run) VALUES (?,?,?,?,?,?,?,?,?)",
               (gid, uid, amount, currency, "deposit" if is_deposit else "withdrawal", category, note, cron_map[period], next_run)); DB.commit()
    await update.message.reply_text(f"🔄 周期复刻: {period} {fmt_amount(amount, currency)} | 下次: {next_run[:10]}")

async def recurring_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    rows = DB.execute("SELECT * FROM recurring WHERE group_id=? AND active=1 ORDER BY next_run", (gid,)).fetchall()
    if not rows:
        await update.message.reply_text("📭 无周期复刻"); return
    msg = "🔄 *周期复刻列表*\n\n"
    for r in rows:
        emoji = "📥" if r["type"] == "deposit" else "📤"
        cat = f" #{r['category']}" if r["category"] else ""
        msg += f"`{r['id']}` {emoji} {r['cron']} {fmt_amount(r['amount'], r['currency'])}{cat} | 下次: {r['next_run'][:10]}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def recurring_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /复刻删 ID"); return
    gid = str(update.effective_chat.id)
    rid = context.args[0]
    DB.execute("UPDATE recurring SET active=0 WHERE id=? AND group_id=?", (rid, gid)); DB.commit()
    await update.message.reply_text(f"🗑 已停用复刻 ID {rid}")

# ---- Export ----
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = str(update.effective_chat.id)
    rows = DB.execute("SELECT * FROM records WHERE group_id=? ORDER BY id", (gid,)).fetchall()
    if not rows:
        await update.message.reply_text("📭 无数据可导出"); return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "群组", "用户", "金额", "币种", "类型", "分类", "备注", "汇率", "时间"])
    for r in rows:
        writer.writerow([r["id"], r["group_id"], r["username"] or r["user_id"], r["amount"],
                        r["currency"], r["type"], r["category"], r["note"], r["usdt_to_cny"], r["created_at"]])
    buf.seek(0)
    await update.message.reply_document(document=io.BytesIO(buf.getvalue().encode()),
                                         filename=f"账本_{gid}_{datetime.now().strftime('%Y%m%d')}.csv",
                                         caption="📎 账单导出")

# ---- Backup ----
async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 仅管理员"); return
    src = f"{DATA_DIR}/bookkeeping.db"
    await update.message.reply_document(document=open(src, "rb"),
                                         filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                                         caption="💾 数据库备份")

# ---- Reminder ----
async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("用法: `/提醒 30 开会` (分钟后)\n`/提醒 18:00 打卡` (时间点)")
        return
    gid, uid = str(update.effective_chat.id), update.effective_user.id
    time_str = context.args[0]
    content = " ".join(context.args[1:])
    from datetime import datetime, timedelta
    now = datetime.now()
    if ":" in time_str:
        h, m = map(int, time_str.split(":"))
        remind_at = now.replace(hour=h, minute=m, second=0)
        if remind_at < now: remind_at += timedelta(days=1)
    else:
        remind_at = now + timedelta(minutes=int(time_str))
    remind_at_str = remind_at.strftime("%Y-%m-%d %H:%M:%S")
    DB.execute("INSERT INTO reminders (group_id, user_id, remind_at, content) VALUES (?,?,?,?)",
               (gid, uid, remind_at_str, content)); DB.commit()
    await update.message.reply_text(f"⏰ 提醒已设: {remind_at.strftime('%m/%d %H:%M')} — {content}")

# ---- Compare ----
async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """对比两个时期"""
    gid = str(update.effective_chat.id)
    # 默认：本月 vs 上月
    now = datetime.now()
    this_start = now.strftime("%Y-%m-01")
    last_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
    rate = get_rate()
    
    def period_stats(start):
        row = DB.execute("""
            SELECT type, SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END), COUNT(*)
            FROM records WHERE group_id=? AND created_at>=? GROUP BY type
        """, (rate, gid, start)).fetchall()
        dep, wit = 0, 0
        for r in row:
            if r[0] == "deposit": dep = r[1] or 0
            else: wit = r[1] or 0
        return dep, wit
    
    this_dep, this_wit = period_stats(this_start)
    last_dep, last_wit = period_stats(last_start)
    
    this_net = this_dep - this_wit
    last_net = last_dep - last_wit
    change = ((this_net - last_net) / abs(last_net) * 100) if last_net != 0 else 0
    arrow = "📈" if change > 0 else "📉"
    
    msg = f"📊 *月度对比*\n\n"
    msg += f"*本月*: 充{this_dep:,.0f} / 下{this_wit:,.0f} / 净{this_net:,.0f} CNY\n"
    msg += f"*上月*: 充{last_dep:,.0f} / 下{last_wit:,.0f} / 净{last_net:,.0f} CNY\n"
    msg += f"{arrow} 变化: {change:+.1f}%\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---- Callback Handler ----
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    
    if data == "refresh_rate":
        global RATE_MANUAL
        RATE_MANUAL = False; RATE_CACHE["time"] = 0
        raw, display = get_live_rate(); current = get_rate()
        await query.edit_message_text(
            f"💱 *实时汇率* [🤖自动]\n裸价: {raw:.4f} | 报价: *{current:.4f}*\n+{SPREAD} | 刚刷新",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="refresh_rate")]]))
    
    elif data == "global_stats":
        msg, kb = global_stats_message()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="global_refresh")]])
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    
    elif data == "global_refresh":
        msg, kb = global_stats_message()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="global_refresh")]])
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    
    elif data.startswith("recent_"):
        parts = data.split("_")
        gid = parts[1]; limit = int(parts[2]) if len(parts) > 2 else 10
        rows = DB.execute("SELECT * FROM records WHERE group_id=? ORDER BY id DESC LIMIT ?", (gid, limit)).fetchall()
        rate = get_rate()
        if not rows:
            await query.edit_message_text("📭 暂无记录"); return
        msg = f"📟 *最新{len(rows)}笔*\n\n"
        for r in rows:
            emoji = "📥" if r["type"] == "deposit" else "📤"
            amt = r["amount"]
            amt_display = f"{amt} {r['currency']}" if r["currency"] == "CNY" else f"{amt} USDT (≈{amt*rate:,.0f}CNY)"
            cat = f" #{r['category']}" if r["category"] else ""
            user = r["username"] or str(r["user_id"])
            msg += f"`{r['id']}` {emoji} {amt_display}{cat} — {user}\n"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data=f"recent_{gid}_{limit}")]])
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    
    elif data.startswith("refresh_"):
        parts = data.split("_")
        gid = parts[1]; period = parts[2] if len(parts) > 2 else "today"
        title = {"today": "📊 今日统计", "week": "📅 本周统计", "month": "📅 本月统计"}.get(period, "📊 统计")
        msg, kb = stats_message(gid, title, period)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    
    elif data.startswith("chart_"):
        gid = data.split("_")[1]
        await query.edit_message_text(text_chart(gid), parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data=f"chart_{gid}")]]))
    
    elif data.startswith("stats_"):
        gid = str(query.message.chat.id)
        period = data.split("_")[1]
        title = {"today": "📊 今日统计", "week": "📅 本周统计", "month": "📅 本月统计"}.get(period, "📊 统计")
        msg, kb = stats_message(gid, title, period)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    
    elif data == "help_full":
        help_text = (
            "📚 *完整帮助*\n\n"
            "📝 *记账*: `+1000` `下发500usdt` `50 #餐饮 外卖`\n"
            "📊 *统计*: /今日 /本周 /本月 /对比\n"
            "📋 *账单*: /账单 [N] /搜索 关键词 /排行\n"
            "🗑 *删除*: /删除 ID\n"
            "💱 *汇率*: /汇率 /汇率更 /汇率设\n"
            "📢 *群发*: /群发 内容 (管理员)\n"
            "📝 *备忘*: /备忘 /备忘录\n"
            "⏰ *提醒*: /提醒 30 开会\n"
            "💰 *预算*: /预算设 /预算查\n"
            "🏦 *债务*: /债务 /债务查\n"
            "🔄 *复刻*: /复刻 /复刻查 /复刻删\n"
            "📈 *图表*: /图表\n"
            "📎 *导出*: /导出\n"
            "💾 *备份*: /备份 (管理员)\n"
            "🌐 *全局*: /全局"
        )
        await query.edit_message_text(help_text, parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_start")]]))
    
    elif data == "back_start":
        await query.edit_message_text("🤖 用 /帮助 查看完整菜单")

# ---- Message Handler ----
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    result = parse_amount(text)
    if result[0] is None:
        return  # not a记账 message
    amount, currency, is_deposit, category, note = result
    gid = str(update.effective_chat.id)
    user = update.effective_user
    uid = user.id; username = user.username or user.full_name
    rate = get_rate()
    
    if not is_deposit:
        # Check budget
        budgets = DB.execute("SELECT * FROM budgets WHERE group_id=? AND category=? AND period=?", (gid, category, "monthly")).fetchall()
        if budgets:
            now = datetime.now()
            b = budgets[0]
            start = now.strftime("%Y-%m-01")
            row = DB.execute("SELECT SUM(CASE WHEN currency='CNY' THEN amount ELSE amount*? END) FROM records WHERE group_id=? AND type='withdrawal' AND category=? AND created_at>=?",
                             (rate, gid, category, start)).fetchone()
            spent = (row[0] or 0)
            if spent + amount > b["amount"]:
                await update.message.reply_text(f"⚠️ *预算预警!*\n{category}: {spent+amount:,.0f}/{b['amount']:,.0f} CNY\n已超 {spent+amount-b['amount']:,.0f} CNY",
                                                parse_mode=ParseMode.MARKDOWN)
    
    DB.execute("INSERT INTO records (group_id, user_id, username, amount, currency, type, category, note, usdt_to_cny) VALUES (?,?,?,?,?,?,?,?,?)",
               (gid, uid, username, amount, currency, "deposit" if is_deposit else "withdrawal", category, note, rate))
    DB.commit()
    rid = DB.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    emoji = "📥" if is_deposit else "📤"
    label = "充值" if is_deposit else "下发"
    disp = fmt_amount(amount, currency)
    if currency == "USDT":
        disp += f" (≈{amount*rate:,.2f} CNY)"
    cat_str = f" #{category}" if category else ""
    note_str = f" — {note}" if note else ""
    await update.message.reply_text(f"{emoji} #{rid} {label} *{disp}*{cat_str}{note_str}",
                                    parse_mode=ParseMode.MARKDOWN)

# ---- Chinese Alias Router ----
CHINESE_ALIASES = {
    "今日": stats_today, "昨天": lambda u,c: stats_today(u,c), "本周": stats_week, "本月": stats_month,
    "账单": bill_list, "排行": ranking, "删除": delete_bill, "删": delete_bill,
    "汇率": show_rate, "汇率更": refresh_rate, "汇率设": set_rate,
    "群发": broadcast, "备忘": memo_add, "备忘录": memo_list,
    "导出": export_csv, "备份": backup_db, "提醒": remind,
    "帮助": lambda u,c: start(u,c), "全局": show_global, "图表": view_chart,
    "搜索": search_records, "对比": compare,
    "预算设": budget_set, "预算查": budget_check,
    "债务": debt_add, "债务查": debt_list,
    "复刻": recurring_add, "复刻查": recurring_list, "复刻删": recurring_del,
}

async def route_chinese(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("/"):
        await handle_message(update, context); return
    cmd = text[1:].split()[0].split("@")[0]
    parts = text.split(); context.args = parts[1:] if len(parts) > 1 else []
    if cmd in CHINESE_ALIASES:
        await CHINESE_ALIASES[cmd](update, context)
    else:
        await handle_message(update, context)

# ====================== MAIN ======================
def recurring_checker(app):
    """后台线程：检查周期复刻"""
    while True:
        time.sleep(60)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = DB.execute("SELECT * FROM recurring WHERE next_run<=? AND active=1", (now,)).fetchall()
            for r in rows:
                rate = get_rate()
                DB.execute("INSERT INTO records (group_id, user_id, username, amount, currency, type, category, note, usdt_to_cny) VALUES (?,?,?,?,?,?,?,?,?)",
                           (r["group_id"], r["user_id"], "⏰自动", r["amount"], r["currency"], r["type"], r["category"], r["note"], rate))
                DB.commit()
                # Update next_run
                from datetime import datetime as dt
                next_dt = dt.now()
                if r["cron"] == "daily": next_dt += timedelta(days=1)
                elif r["cron"] == "weekly": next_dt += timedelta(days=7)
                else: next_dt += timedelta(days=30)
                DB.execute("UPDATE recurring SET next_run=? WHERE id=?", (next_dt.strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
                DB.commit()
                emoji = "📥" if r["type"] == "deposit" else "📤"
                try:
                    await app.bot.send_message(int(r["group_id"]),
                        f"{emoji} ⏰ *周期复刻*\n{fmt_amount(r['amount'], r['currency'])}",
                        parse_mode=ParseMode.MARKDOWN)
                except: pass
        except Exception as e:
            pass

async def reminder_checker(app):
    """后台线程：检查提醒"""
    while True:
        await __import__('asyncio').sleep(30)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = DB.execute("SELECT * FROM reminders WHERE remind_at<=? AND done=0", (now,)).fetchall()
            for r in rows:
                try:
                    await app.bot.send_message(int(r["group_id"]),
                        f"⏰ *提醒*\n{r['content']}",
                        parse_mode=ParseMode.MARKDOWN)
                except: pass
                DB.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],)); DB.commit()
        except: pass

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN required!"); sys.exit(1)
    app = Application.builder().token(TOKEN).build()

    # Latin commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", lambda u,c: start(u,c)))
    app.add_handler(CommandHandler("today", stats_today))
    app.add_handler(CommandHandler("week", stats_week))
    app.add_handler(CommandHandler("month", stats_month))
    app.add_handler(CommandHandler("list", bill_list))
    app.add_handler(CommandHandler("search", search_records))
    app.add_handler(CommandHandler("rank", ranking))
    app.add_handler(CommandHandler("del", delete_bill))
    app.add_handler(CommandHandler("rate", show_rate))
    app.add_handler(CommandHandler("rater", refresh_rate))
    app.add_handler(CommandHandler("rateset", set_rate))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("memo", memo_add))
    app.add_handler(CommandHandler("memos", memo_list))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CommandHandler("backup", backup_db))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("budget", budget_set))
    app.add_handler(CommandHandler("budgetc", budget_check))
    app.add_handler(CommandHandler("debt", debt_add))
    app.add_handler(CommandHandler("debtc", debt_list))
    app.add_handler(CommandHandler("recur", recurring_add))
    app.add_handler(CommandHandler("recurc", recurring_list))
    app.add_handler(CommandHandler("recurd", recurring_del))
    app.add_handler(CommandHandler("global", show_global))
    app.add_handler(CommandHandler("chart", view_chart))
    app.add_handler(CommandHandler("compare", compare))
    # Callback + Message
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_chinese))

    # Background jobs
    import asyncio
    loop = asyncio.new_event_loop()
    loop.create_task(reminder_checker(app))

    # Simple HTTP health-check for Railway (port from env or 8080)
    PORT = int(os.environ.get("PORT", 8080))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *a): pass
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"🤖 超级记账机器人 v2.0 启动... (健康检查端口: {PORT})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()