// 超级记账机器人 - Cloudflare Workers版
// 部署到 Cloudflare Workers + D1数据库

const SPREAD = 0.015;
const BOT_TOKEN = '8638712861:AAFaWkeCAfQp07cCrUi3YX8QFWMGf-7_gGM';
const ADMIN_IDS = new Set([]); // 从环境变量读取

// ====================== HELPERS ======================
function tg(method, body) {
  return fetch(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json());
}

function send(chat_id, text, extra = {}) {
  return tg('sendMessage', { chat_id, text, parse_mode: 'Markdown', ...extra });
}

function reply(update, text, extra = {}) {
  return send(update.message.chat.id, text, { reply_to_message_id: update.message.message_id, ...extra });
}

function isAdmin(userId) {
  if (ADMIN_IDS.size === 0) return false;
  return ADMIN_IDS.has(userId);
}

// ====================== DATABASE via D1 ======================
// D1 binding available as env.DB

async function initDB(db) {
  await db.batch([
    `CREATE TABLE IF NOT EXISTS bills (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      group_id TEXT, user_id INTEGER, username TEXT,
      type TEXT, category TEXT, amount REAL, currency TEXT DEFAULT 'CNY',
      note TEXT, created_at TEXT DEFAULT (datetime('now'))
    )`,
    `CREATE TABLE IF NOT EXISTS rate (id INTEGER PRIMARY KEY AUTOINCREMENT, rate REAL, created_at TEXT DEFAULT (datetime('now')))`,
    `CREATE TABLE IF NOT EXISTS memos (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, user_id INTEGER, content TEXT, created_at TEXT DEFAULT (datetime('now')))`,
    `CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, from_user INTEGER, to_user INTEGER, to_username TEXT, amount REAL, note TEXT, paid INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))`,
    `CREATE TABLE IF NOT EXISTS recurring (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, user_id INTEGER, category TEXT, amount REAL, currency TEXT, cron TEXT, next_run TEXT, created_at TEXT DEFAULT (datetime('now')))`,
    `CREATE TABLE IF NOT EXISTS budgets (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, user_id INTEGER, category TEXT, limit_amount REAL, period TEXT DEFAULT 'month', created_at TEXT DEFAULT (datetime('now')))`,
    `CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT UNIQUE, title TEXT, joined_at TEXT DEFAULT (datetime('now')))`,
    `INSERT OR IGNORE INTO rate (rate) VALUES (7.2)`
  ]);
  return db;
}

async function getRate(db) {
  const r = await db.prepare('SELECT rate FROM rate ORDER BY id DESC LIMIT 1').first();
  return r ? r.rate : 7.2;
}

async function recordBill(db, group_id, user_id, username, type, category, amount, currency, note) {
  await db.prepare(
    'INSERT INTO bills (group_id, user_id, username, type, category, amount, currency, note) VALUES (?,?,?,?,?,?,?,?)'
  ).bind(group_id, user_id, username, type, category, amount, currency, note || '').run();
}

// ====================== MESSAGE PARSING ======================
function parseMessage(text) {
  let type = 'expense';
  let currency = 'CNY';
  let amount = null;
  let category = '';
  let note = '';

  // Check for 充值/下发 keywords
  if (/^(充值|入款)\s*/.test(text)) {
    type = 'income';
    text = text.replace(/^(充值|入款)\s*/, '');
  } else if (/^(下发|提现|支出)\s*/.test(text)) {
    type = 'expense';
    text = text.replace(/^(下发|提现|支出)\s*/, '');
  }

  // Check USDT
  if (/usdt$/i.test(text.trim())) {
    currency = 'USDT';
    text = text.replace(/usdt$/i, '');
  }

  // Parse amount
  const numMatch = text.match(/^([+-]?\d+\.?\d*)/);
  if (numMatch) {
    if (numMatch[1].startsWith('+') || numMatch[1].startsWith('-')) {
      type = numMatch[1].startsWith('+') ? 'income' : 'expense';
      amount = parseFloat(numMatch[1]);
    } else {
      amount = parseFloat(numMatch[1]);
    }
    text = text.replace(numMatch[0], '').trim();
  } else {
    // Check for full Chinese: 充值1000
    const cnMatch = text.match(/(充值|下发|入款|提现|收入|支出)\s*(-?\d+\.?\d*)/);
    if (cnMatch) {
      if (/充值|入款|收入/.test(cnMatch[1])) type = 'income';
      else type = 'expense';
      amount = parseFloat(cnMatch[2]);
      text = text.replace(cnMatch[0], '').trim();
    }
  }

  // Parse category
  const catMatch = text.match(/#(\S+)/);
  if (catMatch) {
    category = catMatch[1];
    text = text.replace(catMatch[0], '').trim();
  }

  note = text;
  return { type, currency, amount, category, note };
}

// ====================== STATS ======================
async function getStats(db, group_id, period) {
  let whereClause = '';
  switch (period) {
    case 'today': whereClause = "date(created_at) = date('now')"; break;
    case 'yesterday': whereClause = "date(created_at) = date('now', '-1 day')"; break;
    case 'week': whereClause = "date(created_at) >= date('now', '-7 days')"; break;
    case 'month': whereClause = "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"; break;
    default: whereClause = "date(created_at) = date('now')";
  }

  const rate = await getRate(db);
  
  const stmt = `SELECT type, currency, SUM(amount) as total, COUNT(*) as count 
    FROM bills WHERE group_id = ? AND ${whereClause} GROUP BY type, currency`;
  const rows = await db.prepare(stmt).bind(group_id).all();
  
  let incomeCNY = 0, incomeUSDT = 0, expCNY = 0, expUSDT = 0;
  let incCount = 0, expCount = 0;
  
  for (const r of rows.results) {
    if (r.type === 'income') {
      incCount += r.count;
      if (r.currency === 'USDT') incomeUSDT += r.total;
      else incomeCNY += r.total;
    } else {
      expCount += r.count;
      if (r.currency === 'USDT') expUSDT += r.total;
      else expCNY += r.total;
    }
  }
  
  const totalIncome = incomeCNY + incomeUSDT * rate;
  const totalExp = expCNY + expUSDT * rate;
  
  return { incomeCNY, incomeUSDT, expCNY, expUSDT, incCount, expCount, totalIncome, totalExp, rate };
}

// ====================== HANDLERS ======================
async function handleStart(update, db) {
  const chat = update.message.chat;
  const group_id = chat.id.toString();
  await db.prepare('INSERT OR IGNORE INTO groups (group_id, title) VALUES (?,?)')
    .bind(group_id, chat.title || chat.username || '私聊').run();
  
  const text = `🤖 *超级记账机器人*\n\n💰 记账方式:\n\`100 #餐饮 午餐\` — 支出\n\`+500 #工资\` — 收入\n\`100usdt\` — USDT记账\n\`充值1000\` / \`下发500\`\n\n📊 /今日 /本月 /账单 /排行 /图表\n🔍 /搜xxx /汇率 /全局\n📝 /备忘录 /提醒 /预算 /债务\n\n输入 /help 看全部`;
  return reply(update, text);
}

async function handleMessage(update, db) {
  const msg = update.message;
  if (!msg || !msg.text) return;
  
  const text = msg.text.trim();
  const chat_id = msg.chat.id.toString();
  const user_id = msg.from.id;
  const username = msg.from.username || msg.from.first_name || '';
  
  // Ensure group exists
  await db.prepare('INSERT OR IGNORE INTO groups (group_id, title) VALUES (?,?)')
    .bind(chat_id, msg.chat.title || msg.chat.username || '私聊').run();

  // Parse commands
  if (text.startsWith('/')) {
    return handleCommand(update, db);
  }

  // Parse as bill entry
  const parsed = parseMessage(text);
  if (parsed.amount === null) return; // Not a bill

  await recordBill(db, chat_id, user_id, username, parsed.type, parsed.category, parsed.amount, parsed.currency, parsed.note);
  
  const amountStr = parsed.amount.toFixed(parsed.currency === 'USDT' ? 2 : 0);
  const typeEmoji = parsed.type === 'income' ? '💰' : '💸';
  const categoryStr = parsed.category ? ` #${parsed.category}` : '';
  const replyText = `${typeEmoji} ${parsed.type === 'income' ? '入账' : '支出'} ${amountStr} ${parsed.currency}${categoryStr}`;
  return reply(update, replyText);
}

async function handleCommand(update, db) {
  const msg = update.message;
  const chat_id = msg.chat.id.toString();
  const user_id = msg.from.id;
  const text = msg.text;
  const parts = text.split(/\s+/);
  const cmd = parts[0].split('@')[0].toLowerCase();
  const args = parts.slice(1);

  // Normalize Chinese commands
  const cmdMap = {
    '/今日': 'today', '/昨天': 'yesterday', '/本周': 'week', '/本月': 'month',
    '/账单': 'list', '/排行': 'rank', '/删': 'del', '/汇率': 'rate',
    '/群发': 'broadcast', '/备忘录': 'memos', '/导出': 'export',
    '/群组': 'groups', '/加管理': 'addadmin', '/提醒': 'remind',
    '/帮助': 'help', '/最新': 'recent', '/全局': 'global',
    '/搜': 'search', '/图表': 'chart', '/对比': 'compare',
    '/预算': 'budget', '/债务': 'debt', '/欠款': 'debts',
    '/复刻': 'recur', '/备份': 'backup',
  };
  const action = cmdMap[cmd] || cmd.replace('/', '');

  switch (action) {
    case 'start': return handleStart(update, db);
    case 'help': case '帮助':
      return reply(update, `🤖 *超级记账机器人*\n\n📊 /今日 /本月 /账单 /排行 /图表\n🔍 /搜 /汇率 /全局 /最新\n📝 /备忘录 /提醒 /预算 /债务\n💰 记账: \`100 #餐饮 午餐\` 或 \`充值1000\``);

    case 'today': case 'yesterday': case 'week': case 'month': {
      const stats = await getStats(db, chat_id, action);
      const periodLabel = { today: '今日', yesterday: '昨天', week: '本周', month: '本月' };
      let txt = `📊 *${periodLabel[action]}统计*\n\n`;
      txt += `💰 入账: ${stats.incCount}笔 / ${stats.incomeCNY.toFixed(0)} CNY`;
      if (stats.incomeUSDT > 0) txt += ` + ${stats.incomeUSDT.toFixed(2)} USDT`;
      txt += `\n💸 支出: ${stats.expCount}笔 / ${stats.expCNY.toFixed(0)} CNY`;
      if (stats.expUSDT > 0) txt += ` + ${stats.expUSDT.toFixed(2)} USDT`;
      txt += `\n💱 汇率: ${stats.rate}`;
      txt += `\n📈 净额: ${(stats.totalIncome - stats.totalExp).toFixed(0)} CNY`;
      return reply(update, txt);
    }

    case 'rate': {
      const rate = await getRate(db);
      const display = rate + SPREAD;
      if (isAdmin(user_id)) {
        return reply(update, `💱 裸价: ${rate} CNY\n📈 报价: ${display} CNY (上浮${SPREAD})`);
      }
      return reply(update, `💱 1 USDT = ${display} CNY`);
    }

    case 'list': case '账单': {
      const rows = await db.prepare(
        `SELECT * FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 20`
      ).bind(chat_id).all();
      if (!rows.results.length) return reply(update, '📭 暂无账单');
      let txt = '📋 *最近账单*\n\n';
      for (const r of rows.results) {
        const emoji = r.type === 'income' ? '💰' : '💸';
        txt += `${emoji} #${r.id} ${r.amount} ${r.currency}`;
        if (r.category) txt += ` #${r.category}`;
        if (r.note) txt += ` ${r.note}`;
        txt += '\n';
      }
      txt += '\n删记录: /删 ID';
      return reply(update, txt);
    }

    case 'rank': case '排行': {
      const rows = await db.prepare(
        `SELECT category, SUM(amount) as total, COUNT(*) as cnt FROM bills 
         WHERE group_id = ? AND type = 'expense' AND category != '' 
         AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
         GROUP BY category ORDER BY total DESC LIMIT 15`
      ).bind(chat_id).all();
      if (!rows.results.length) return reply(update, '📭 本月无支出分类');
      let txt = '🏆 *本月支出排行*\n\n';
      rows.results.forEach((r, i) => {
        txt += `${i + 1}. #${r.category} — ${r.total.toFixed(0)} CNY (${r.cnt}笔)\n`;
      });
      return reply(update, txt);
    }

    case 'search': case '搜': {
      if (!args.length) return reply(update, '用法: /搜 关键词');
      const kw = `%${args.join(' ')}%`;
      const rows = await db.prepare(
        `SELECT * FROM bills WHERE group_id = ? AND (note LIKE ? OR category LIKE ?) ORDER BY id DESC LIMIT 20`
      ).bind(chat_id, kw, kw).all();
      if (!rows.results.length) return reply(update, `🔍 未找到 "${args.join(' ')}"`);
      let txt = `🔍 *搜索 "${args.join(' ')}"*\n\n`;
      rows.results.forEach(r => {
        txt += `#${r.id} ${r.type === 'income' ? '💰' : '💸'} ${r.amount} ${r.currency}`;
        if (r.category) txt += ` #${r.category}`;
        if (r.note) txt += ` ${r.note}`;
        txt += '\n';
      });
      return reply(update, txt);
    }

    case 'global': case '全局': {
      const rate = await getRate(db);
      const rows = await db.prepare(
        `SELECT g.title, g.group_id,
          (SELECT COALESCE(SUM(CASE WHEN currency='USDT' THEN amount*? ELSE amount END),0) FROM bills WHERE group_id=g.group_id AND type='income') as income,
          (SELECT COALESCE(SUM(CASE WHEN currency='USDT' THEN amount*? ELSE amount END),0) FROM bills WHERE group_id=g.group_id AND type='expense') as expense,
          (SELECT COUNT(*) FROM bills WHERE group_id=g.group_id) as total_bills
         FROM groups g ORDER BY income DESC`
      ).bind(rate, rate).all();
      if (!rows.results.length) return reply(update, '📭 无数据');
      let txt = '🌐 *全局统计*\n\n';
      for (const r of rows.results) {
        txt += `📥 ${r.title || r.group_id}\n`;
        txt += `   💰入: ${r.income.toFixed(0)} | 💸出: ${r.expense.toFixed(0)} | 余额: ${(r.income - r.expense).toFixed(0)}\n`;
      }
      return reply(update, txt);
    }

    case 'del': case '删': {
      if (!args.length) return reply(update, '用法: /删 记录ID');
      const id = parseInt(args[0]);
      await db.prepare('DELETE FROM bills WHERE id = ? AND group_id = ?').bind(id, chat_id).run();
      return reply(update, `🗑 已删除 #${id}`);
    }

    case 'memo': {
      if (!args.length) return reply(update, '用法: /memo 内容');
      const content = args.join(' ');
      await db.prepare('INSERT INTO memos (group_id, user_id, content) VALUES (?,?,?)')
        .bind(chat_id, user_id, content).run();
      return reply(update, `📝 已备忘: ${content}`);
    }

    case 'memos': case '备忘录': {
      const rows = await db.prepare(
        'SELECT * FROM memos WHERE group_id = ? ORDER BY id DESC LIMIT 20'
      ).bind(chat_id).all();
      if (!rows.results.length) return reply(update, '📭 暂无备忘');
      let txt = '📝 *备忘录*\n\n';
      rows.results.forEach((r, i) => {
        txt += `${i + 1}. ${r.content}\n`;
      });
      return reply(update, txt);
    }

    case 'broadcast': case '群发': {
      if (!isAdmin(user_id)) {
        return reply(update, '❌ 无权限');
      }
      if (!args.length) return reply(update, '用法: /群发 内容');
      const content = args.join(' ');
      const groups = await db.prepare('SELECT group_id FROM groups').all();
      let sent = 0;
      for (const g of groups.results) {
        try {
          await send(parseInt(g.group_id), `📢 *群发通知*\n\n${content}`);
          sent++;
        } catch (e) {}
      }
      return reply(update, `📢 已发送到 ${sent}/${groups.results.length} 个群`);
    }

    case 'groups': case '群组': {
      const rows = await db.prepare('SELECT * FROM groups').all();
      let txt = '👥 *群组列表*\n\n';
      for (const r of rows.results) {
        txt += `📥 ${r.title || r.group_id}\n`;
      }
      txt += `\n共 ${rows.results.length} 个群`;
      return reply(update, txt);
    }

    default:
      // Don't reply to unknown commands to avoid noise
      break;
  }
}

// ====================== MAIN WORKER ======================
export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') return new Response('OK');

    try {
      const update = await request.json();
      
      // Initialize DB
      const db = env.DB;
      
      // Process update
      if (update.message && update.message.text) {
        await handleMessage(update, db);
      } else if (update.callback_query) {
        // Handle callbacks
        const cb = update.callback_query;
        if (cb.data === 'refresh_rate') {
          // Simple callback
          const rate = await getRate(db);
          const display = rate + SPREAD;
          await tg('answerCallbackQuery', { callback_query_id: cb.id });
          await tg('editMessageText', {
            chat_id: cb.message.chat.id,
            message_id: cb.message.message_id,
            text: `💱 1 USDT = ${display} CNY\n(裸价: ${rate} + ${SPREAD})`,
            parse_mode: 'Markdown'
          });
        }
      }
      
      return new Response('OK');
    } catch (e) {
      return new Response('Error: ' + e.message, { status: 500 });
    }
  }
};