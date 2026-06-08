# 📊 Telegram 多币种记账机器人

## 功能
- 💰 **多币种记账**: CNY / USDT，自动汇率换算
- 👥 **多群多账单**: 每群账单独立统计
- 📢 **群发通知**: 一键群发到所有群
- 📊 **统计分析**: 今日/本周/本月/排行
- 📝 **备忘录**: 随手记录
- ⏰ **提醒**: 定时提醒
- 📎 **导出CSV**: 一键导出账单
- 🔐 **权限管理**: 分级管理员

## 部署 (Render.com 免费)

### 1. Fork 本仓库到你的 GitHub

### 2. Render 部署
1. 注册 [render.com](https://render.com)
2. New → Web Service → 连接你的 GitHub
3. 选择本仓库
4. Runtime: Worker
5. Build Command: `pip install -r requirements.txt`
6. Start Command: `python bot.py`
7. 添加环境变量:
   - `BOT_TOKEN`: 你的Telegram Bot Token
   - `ADMIN_IDS`: 你的Telegram用户ID（逗号分隔）

### 3. 获取你的 Telegram 用户 ID
找 @userinfobot 发消息获取

## 本地部署
```bash
pip install -r requirements.txt
BOT_TOKEN=xxx ADMIN_IDS=123456 python bot.py
```

## 使用帮助
机器人启动后在群里发 `/start` 查看完整帮助