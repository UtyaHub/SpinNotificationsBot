import asyncio
from asyncio import Semaphore
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, ChatMemberUpdated, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
)

from config import TELEGRAM_TOKEN, STREAMERS, CHECK_INTERVAL, INACTIVE_CHECK_INTERVAL, URL_TEMPLATE
from monitor import StreamMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROXY_URL = "socks5://127.0.0.1:10808"
CHAT_IDS_FILE = Path(__file__).parent / ".chat_ids"
PENDING_FILE = Path(__file__).parent / "pending_streamers.json"
STATES_FILE = Path(__file__).parent / "roulette_states.json"
ADMIN_CHAT_ID = 292141127
BACKUP_DIR = Path(__file__).parent / "backups"

CB_APPROVE = "submit_approve:"
CB_REJECT = "submit_reject:"


# --- JSON helpers ---

def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default if default is not None else {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Chat IDs ---

def load_chat_ids() -> set[int]:
    ids = set()
    if CHAT_IDS_FILE.exists():
        for line in CHAT_IDS_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    ids.add(int(line))
                except ValueError:
                    pass
    return ids


def save_chat_ids(ids: set[int]):
    # Safety: don't overwrite if we loaded nothing but file has data
    if not ids and CHAT_IDS_FILE.exists():
        existing = load_chat_ids()
        if existing:
            logger.warning("Refusing to save empty chat_ids - file has data")
            return
    CHAT_IDS_FILE.write_text("\n".join(str(i) for i in sorted(ids)))


# --- Extra streamers ---

def load_extra_streamers() -> list[str]:
    data = load_json(PENDING_FILE, [])
    return data if isinstance(data, list) else []


def save_extra_streamers(extra: list[str]):
    save_json(PENDING_FILE, extra)


# --- Roulette states ---

def load_roulette_states() -> dict:
    return load_json(STATES_FILE, {})


def save_roulette_states(states: dict):
    save_json(STATES_FILE, states)



# --- Backup ---

BACKUP_FILES = [
    (".chat_ids", ".chat_ids"),
    ("pending_streamers.json", "pending_streamers.json"),
    ("roulette_states.json", "roulette_states.json"),
    ("config.py", "config.py"),
]


def backup_data():
    import shutil
    from datetime import datetime
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for src_name, dst_name in BACKUP_FILES:
        src = Path(__file__).parent / src_name
        if src.exists():
            dst = BACKUP_DIR / f"{dst_name}.{ts}.bak"
            shutil.copy2(src, dst)
    # Keep only last 24 backups per file
    for src_name, _ in BACKUP_FILES:
        backups = sorted(BACKUP_DIR.glob(f"{src_name}.*.bak"))
        for old in backups[:-24]:
            old.unlink()
    logger.info(f"Backup completed: {ts}")


# --- Streamer management ---

def add_streamer(nickname: str):
    if nickname not in STREAMERS:
        STREAMERS.append(nickname)
        existing = load_extra_streamers()
        if nickname not in existing:
            existing.append(nickname)
            save_extra_streamers(existing)
        logger.info(f"Added streamer: {nickname}")


# --- Timer helpers ---

def timer_to_minutes_str(timer: str | None) -> str:
    if not timer:
        return ""
    parts = timer.split(":")
    if len(parts) != 3:
        return ""
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        total_min = h * 60 + m
        if s > 0:
            total_min += 1
        if total_min <= 0:
            return "менее 1 минуты"
        return f"~{total_min} мин."
    except ValueError:
        return ""


def timer_to_seconds(timer: str | None) -> int:
    if not timer:
        return 999999
    parts = timer.split(":")
    if len(parts) != 3:
        return 999999
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 999999


def format_timer_line(timer: str | None) -> str:
    timer_min = timer_to_minutes_str(timer)
    if timer_min:
        return f"\n⏱ Осталось {timer_min}"
    return ""


# --- Notification text builders ---

def build_start_notification(nickname: str, url: str, timer: str | None) -> str:
    min_line = format_timer_line(timer)
    return (
        "🎰 РУЛЕТКА ЗАПУЩЕНА!\n\n"
        f"Стример: {nickname}\n"
        f"Ссылка: {url}{min_line}\n\n"
        "Успей поучаствовать!"
    )


# --- URL parsing ---

def extract_nickname(url: str) -> str:
    clean = url.split("?")[0].split("#")[0]
    return clean.rstrip("/").split("/")[-1]


# --- Globals ---

router = Router()
monitor = StreamMonitor()
chat_ids: set[int] = load_chat_ids()
muted_chats: set[int] = set()
last_cycle_time = {"time": ""}
last_backup_time = 0

_extra = load_extra_streamers()
for s in _extra:
    if s not in STREAMERS:
        STREAMERS.append(s)

_pending: dict = {}


# --- Notify ---

async def notify_all(bot: Bot, text: str):
    for cid in list(chat_ids):
        if cid in muted_chats:
            continue
        try:
            await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to send to {cid}: {e}")


# --- Handlers ---

@router.my_chat_member()
async def on_chat_member(event: ChatMemberUpdated):
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    if new_status in ("member", "administrator") and old_status in ("left", "kicked"):
        chat_ids.add(event.chat.id)
        save_chat_ids(chat_ids)
        logger.info(f"Bot added to chat: {event.chat.id} ({event.chat.title})")
        await event.answer(
            "Бот запущен! Уведомления о рулетках будут приходить сюда.\n\n"
            f"Отслеживаю {len(STREAMERS)} стримеров, интервал {CHECK_INTERVAL} сек."
        )
    elif new_status in ("left", "kicked") and old_status in ("member", "administrator"):
        chat_ids.discard(event.chat.id)
        save_chat_ids(chat_ids)
        logger.info(f"Bot removed from chat: {event.chat.id}")


@router.message(CommandStart())
async def cmd_start(message: Message):
    chat_ids.add(message.chat.id)
    save_chat_ids(chat_ids)
    logger.info(f"Chat registered: {message.chat.id}")
    await message.answer(
        "Бот мониторинга рулеток BetBoom запущен!\n\n"
        f"Отслеживаю {len(STREAMERS)} стримеров\n"
        f"Интервал проверки: {CHECK_INTERVAL} сек.\n\n"
        "Уведомления будут приходить при запуске рулетки.\n\n"
        "Команды:\n"
        "/status — активные рулетки\n"
        "/list — список стримеров\n"
        "/submit <url> — предложить стримера"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not monitor._states:
        await message.answer("Проверка ещё не выполнялась. Подождите...")
        return

    active = {n: s for n, s in monitor._states.items() if s.get("active")}
    if not active:
        await message.answer("Нет активных рулеток.")
        return

    lines = ["🎰 Активные рулетки:\n"]
    if last_cycle_time["time"]:
        lines.append(f"Обновлено: {last_cycle_time['time']}\n")

    sorted_active = sorted(active.items(), key=lambda x: timer_to_seconds(x[1].get("timer")))
    for nickname, state in sorted_active:
        timer = state.get("timer")
        timer_min = timer_to_minutes_str(timer)
        secs = timer_to_seconds(timer)
        urgent = " ⚡" if secs < 900 else ""
        min_info = f"  (осталось {timer_min})" if timer_min else ""
        lines.append(f"• {nickname}{urgent}{min_info}\n  {URL_TEMPLATE.format(nickname)}")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("list"))
async def cmd_list(message: Message):
    header = "📋 Отслеживаемые стримеры:\n"
    body = ", ".join(STREAMERS)
    await message.answer(f"{header}\n{body}", disable_web_page_preview=True)


@router.message(Command("mute"))
async def cmd_mute(message: Message):
    muted_chats.add(message.chat.id)
    await message.answer("Уведомления отключены. Используйте /unmute чтобы включить обратно.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message):
    muted_chats.discard(message.chat.id)
    await message.answer("Уведомления включены!")



@router.message(Command("all"))
async def cmd_all(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Только админ может использовать эту команду.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите текст:\n/all сообщение для всех")
        return

    text = parts[1].strip()
    sent = 0
    for cid in list(chat_ids):
        try:
            await message.bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send to {cid}: {e}")
    await message.answer(f"Сообщение отправлено {sent} чатам.")

@router.message(Command("submit"))
async def cmd_submit(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите ссылку на стрим:\n"
            "/submit https://betboom.ru/freestream/nickname"
        )
        return

    url = parts[1].strip()
    if not url.startswith("http"):
        await message.answer("Ссылка должна начинаться с http/https.")
        return

    nickname = extract_nickname(url)
    if not nickname:
        await message.answer("Не удалось извлечь никнейм из URL.")
        return

    if nickname.lower() in {s.lower() for s in STREAMERS}:
        await message.answer(f"Стример {nickname} уже отслеживается!")
        return

    # Check if roulette is active - first from cache, then by API
    state = monitor._states.get(nickname)
    if not state or not state.get("active"):
        check_url = URL_TEMPLATE.format(nickname)
        res = await monitor.check_streamer(nickname, check_url)
        if res.get("active"):
            state = {"active": True, "timer": res.get("timer")}
        elif res.get("error"):
            logger.warning(f"Failed to check {nickname}: {res['error']}, sending to admin")
            state = None
        else:
            state = None


    if state and state.get("active"):
        add_streamer(nickname)
        timer = state.get("timer", "")
        await message.answer(
            f"✅ Стример {nickname} автоматически добавлен в мониторинг!\n"
            f"{URL_TEMPLATE.format(nickname)}"
            f"{format_timer_line(timer)}\n\n"
            "Рулетка запущена, успейте поучаствовать!",
            disable_web_page_preview=True,
        )
        # Notify all users
        await notify_all(message.bot, build_start_notification(nickname, URL_TEMPLATE.format(nickname), timer))
        return

    # Roulette not active -> send to admin for approval
    user_name = (
        message.from_user.username
        or message.from_user.first_name
        or str(message.from_user.id)
    )
    _pending[nickname] = {
        "url": url,
        "user_id": message.from_user.id,
        "user_name": user_name,
        "chat_id": message.chat.id,
    }

    try:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"{CB_APPROVE}{nickname}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{CB_REJECT}{nickname}"),
            ]
        ])

        await message.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                "📥 Новая заявка!\n\n"
                f"Стример: {nickname}\n"
                f"Ссылка: {url}\n"
                f"От @{user_name} (ID: {message.from_user.id})"
            ),
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await message.answer("Заявка отправлена админу на одобрение.")
    except Exception as e:
        logger.error(f"Failed to send to admin: {e}")
        await message.answer("Не удалось отправить заявку админу. Попробуйте пожалуйста позже.")


@router.callback_query(lambda c: c.data.startswith(CB_APPROVE) or c.data.startswith(CB_REJECT))
async def handle_submit_callback(callback: CallbackQuery):
    data = callback.data
    nickname = data.split(":", 1)[1]

    if nickname not in _pending:
        await callback.answer("Заявка не найдена или устарела.", show_alert=True)
        return

    sub = _pending.pop(nickname)
    chat_id = sub["chat_id"]
    is_approve = data.startswith(CB_APPROVE)

    if is_approve:
        add_streamer(nickname)
        await callback.message.edit_text(callback.message.text + "\n\n✅ Одобрено!")
        try:
            await callback.bot.send_message(
                chat_id,
                f"✅ Стример {nickname} одобрен и добавлен в мониторинг!",
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    else:
        await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
        try:
            await callback.bot.send_message(chat_id, f"❌ Стример {nickname} отклонен админом.")
        except Exception:
            pass

    await callback.answer()


# --- Monitoring ---

async def check_one(nickname):
    url = URL_TEMPLATE.format(nickname)
    return nickname, url, await monitor.check_streamer(nickname, url)


async def monitoring_loop(bot: Bot):
    global last_cycle_time, last_backup_time
    CONCURRENT_CHECKS = 10
    _last_check: dict[str, float] = {}

    while True:
        if not chat_ids:
            await asyncio.sleep(5)
            continue

        cycle_start = time.monotonic()
        now = cycle_start
        current_streamers = list(STREAMERS)

        # Smart polling: skip streamers checked recently
        to_check = []
        for n in current_streamers:
            state = monitor._states.get(n)
            is_active = state.get("active") if state else None
            interval = CHECK_INTERVAL if is_active else INACTIVE_CHECK_INTERVAL
            last = _last_check.get(n, 0)
            if now - last >= interval:
                to_check.append(n)

        if not to_check:
            await asyncio.sleep(10)
            continue

        for i in range(0, len(to_check), CONCURRENT_CHECKS):
            batch = to_check[i : i + CONCURRENT_CHECKS]

            results = await asyncio.gather(
                *(check_one(n) for n in batch), return_exceptions=True
            )

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Check failed: {result}")
                    continue

                nickname, url, res = result
                _last_check[nickname] = now

                if res.get("error"):
                    logger.error(f"Error checking {nickname}: {res['error']}")
                    continue

                if (res.get("changed")) and res.get("active"):
                    timer = res.get("timer", "")
                    await notify_all(bot, build_start_notification(nickname, url, timer))
                    logger.info(f"Notification sent for {nickname}")

        save_roulette_states(monitor._states)
        last_cycle_time["time"] = datetime.now().strftime("%H:%M:%S")

        elapsed_total = time.monotonic()
        if elapsed_total - last_backup_time >= 3600:
            last_backup_time = elapsed_total
            try:
                backup_data()
            except Exception as e:
                logger.error(f"Backup failed: {e}")

        active_count = sum(1 for v in monitor._states.values() if v.get("active"))
        logger.info(
            f"Cycle: {active_count} active, {len(to_check)}/{len(current_streamers)} checked, "
            f"{time.monotonic() - cycle_start:.1f}s"
        )
        await asyncio.sleep(5)


# --- Main ---

async def main():
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=TELEGRAM_TOKEN, session=session)

    # Load saved states
    saved_states = load_roulette_states()
    if saved_states:
        monitor._states.update(saved_states)
        logger.info(f"Loaded {len(saved_states)} saved roulette states")

    await monitor.start()
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="status", description="Активные рулетки"),
        BotCommand(command="list", description="Список стримеров"),
        BotCommand(command="submit", description="Предложить стримера"),
        BotCommand(command="mute", description="Выключить уведомления"),
        BotCommand(command="unmute", description="Включить уведомления"),
    ])

    asyncio.create_task(monitoring_loop(bot))

    logger.info(f"Bot started. Registered chats: {chat_ids}, streaming {len(STREAMERS)} streamers")
    try:
        await dp.start_polling(bot)
    finally:
        await monitor.stop()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())





