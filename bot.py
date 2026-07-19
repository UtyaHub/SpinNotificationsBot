import asyncio
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
from dotenv import load_dotenv

from config import TELEGRAM_TOKEN, STREAMERS, CHECK_INTERVAL, URL_TEMPLATE
from monitor import StreamMonitor

load_dotenv()

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

CB_APPROVE = "submit_approve:"
CB_REJECT = "submit_reject:"


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
    CHAT_IDS_FILE.write_text("\n".join(str(i) for i in sorted(ids)))


def load_extra_streamers() -> list[str]:
    if PENDING_FILE.exists():
        try:
            data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_extra_streamers(extra: list[str]):
    PENDING_FILE.write_text(
        json.dumps(extra, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_roulette_states() -> dict:
    if STATES_FILE.exists():
        try:
            return json.loads(STATES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_roulette_states(states: dict):
    STATES_FILE.write_text(
        json.dumps(states, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def add_streamer(nickname: str):
    if nickname not in STREAMERS:
        STREAMERS.append(nickname)
        existing = load_extra_streamers()
        if nickname not in existing:
            existing.append(nickname)
            save_extra_streamers(existing)
        logger.info(f"Added streamer: {nickname}")


router = Router()
monitor = StreamMonitor()
chat_ids: set[int] = load_chat_ids()
muted_chats: set[int] = set()

_extra = load_extra_streamers()
for s in _extra:
    if s not in STREAMERS:
        STREAMERS.append(s)

_pending: dict = {}
last_cycle_time = {"time": ""}


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
            return chr(1084)+chr(1077)+chr(1085)+chr(1077)+chr(1077)+" 1 "+chr(1084)+chr(1080)+chr(1085)+chr(1091)+chr(1090)+chr(1099)
        return f"~{total_min} " + chr(1084)+chr(1080)+chr(1085)+chr(1091)+chr(1090)
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


async def notify_all(bot: Bot, text: str):
    for cid in list(chat_ids):
        if cid in muted_chats:
            continue
        try:
            await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to send to {cid}: {e}")


# ---- Handlers ----

@router.my_chat_member()
async def on_chat_member(event: ChatMemberUpdated):
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    if new_status in ("member", "administrator") and old_status in ("left", "kicked"):
        chat_ids.add(event.chat.id)
        save_chat_ids(chat_ids)
        logger.info(f"Bot added to chat: {event.chat.id} ({event.chat.title})")
        await event.answer(
            "\u0411\u043e\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d! \u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u043e \u0440\u0443\u043b\u0435\u0442\u043a\u0430\u0445 \u0431\u0443\u0434\u0443\u0442 \u043f\u0440\u0438\u0445\u043e\u0434\u0438\u0442\u044c \u0441\u044e\u0434\u0430.\n\n"
            f"\u041e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u044e {len(STREAMERS)} \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u043e\u0432, \u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b {CHECK_INTERVAL} \u0441\u0435\u043a."
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
        "\u0411\u043e\u0442 \u043c\u043e\u043d\u0438\u0442\u043e\u0440\u0438\u043d\u0433\u0430 \u0440\u0443\u043b\u0435\u0442\u043e\u043a BetBoom \u0437\u0430\u043f\u0443\u0449\u0435\u043d!\n\n"
        f"\u041e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u044e {len(STREAMERS)} \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u043e\u0432\n"
        f"\u0418\u043d\u0442\u0435\u0440\u0432\u0430\u043b \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438: {CHECK_INTERVAL} \u0441\u0435\u043a.\n\n"
        "\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0431\u0443\u0434\u0443\u0442 \u043f\u0440\u0438\u0445\u043e\u0434\u0438\u0442\u044c \u043f\u0440\u0438 \u0437\u0430\u043f\u0443\u0441\u043a\u0435 \u0440\u0443\u043b\u0435\u0442\u043a\u0438.\n\n"
        "\u041a\u043e\u043c\u0430\u043d\u0434\u044b:\n"
        "/status \u2014 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u0440\u0443\u043b\u0435\u0442\u043a\u0438\n"
        "/list \u2014 \u0441\u043f\u0438\u0441\u043e\u043a \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u043e\u0432\n"
        "/submit <url> \u2014 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0438\u0442\u044c \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u0430"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not monitor._states:
        await message.answer(
            chr(1055)+chr(1088)+chr(1086)+chr(1074)+chr(1077)+chr(1088)+chr(1082)+chr(1072)+" "+chr(1077)+chr(1097)+chr(1105)+" "+chr(1085)+chr(1077)+" "+chr(1074)+chr(1099)+chr(1087)+chr(1086)+chr(1083)+chr(1085)+chr(1103)+chr(1083)+chr(1072)+chr(1089)+chr(1100)+". "+chr(1055)+chr(1086)+chr(1076)+chr(1086)+chr(1078)+chr(1076)+chr(1080)+chr(1090)+chr(1077)+"..."
        )
        return

    active = {n: s for n, s in monitor._states.items() if s.get("active")}
    if not active:
        await message.answer(
            chr(1053)+chr(1077)+chr(1090)+" "+chr(1072)+chr(1082)+chr(1090)+chr(1080)+chr(1074)+chr(1085)+chr(1099)+chr(1093)+" "+chr(1088)+chr(1091)+chr(1083)+chr(1077)+chr(1090)+chr(1086)+chr(1082)+"."
        )
        return

    lines = [
        "\U0001f3b0 "
        + chr(1040)+chr(1082)+chr(1090)+chr(1080)+chr(1074)+chr(1085)+chr(1099)+chr(1077)+" "+chr(1088)+chr(1091)+chr(1083)+chr(1077)+chr(1090)+chr(1082)+chr(1080)+":\n"
    ]
    if last_cycle_time["time"]:
        lines.append(chr(1054)+chr(1073)+chr(1085)+chr(1086)+chr(1074)+chr(1083)+chr(1077)+chr(1085)+chr(1086)+": " + last_cycle_time["time"])
    sorted_active = sorted(active.items(), key=lambda x: timer_to_seconds(x[1].get("timer")))
    for nickname, state in sorted_active:
        timer = state.get("timer")
        timer_min = timer_to_minutes_str(timer)
        secs = timer_to_seconds(timer)
        urgent = " \u26a1" if secs < 900 else ""
        timer_info = f"  \u23f1 {timer}" if timer else ""
        min_info = f"  ({chr(1086)+chr(1089)+chr(1090)+chr(1072)+chr(1083)+chr(1086)+chr(1089)+chr(1100)} {timer_min})" if timer_min else ""
        lines.append(f"\u2022 {nickname}{urgent}{min_info}\n  {URL_TEMPLATE.format(nickname)}")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("list"))
async def cmd_list(message: Message):
    lines = [
        "\U0001f4cb "
        + chr(1054)+chr(1090)+chr(1089)+chr(1083)+chr(1077)+chr(1078)+chr(1080)+chr(1074)+chr(1072)+chr(1077)+chr(1084)+chr(1099)+chr(1077)+" "+chr(1089)+chr(1090)+chr(1088)+chr(1080)+chr(1084)+chr(1077)+chr(1088)+chr(1099)+":\n"
    ]
    lines.append(", ".join(STREAMERS))
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("mute"))
async def cmd_mute(message: Message):
    muted_chats.add(message.chat.id)
    await message.answer(
        chr(1059)+chr(1074)+chr(1077)+chr(1076)+chr(1086)+chr(1084)+chr(1083)+chr(1077)+chr(1085)+chr(1080)+chr(1103)+" "+chr(1086)+chr(1090)+chr(1082)+chr(1083)+chr(1102)+chr(1095)+chr(1077)+chr(1085)+chr(1099)+". "
        +chr(1048)+chr(1089)+chr(1087)+chr(1086)+chr(1083)+chr(1100)+chr(1079)+chr(1091)+chr(1081)+chr(1090)+chr(1077)+" /unmute "
        +chr(1095)+chr(1090)+chr(1086)+chr(1073)+chr(1099)+" "+chr(1074)+chr(1082)+chr(1083)+chr(1102)+chr(1095)+chr(1080)+chr(1090)+chr(1100)+" "+chr(1086)+chr(1073)+chr(1088)+chr(1072)+chr(1090)+chr(1085)+chr(1086)+"."
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message):
    muted_chats.discard(message.chat.id)
    await message.answer(
        chr(1059)+chr(1074)+chr(1077)+chr(1076)+chr(1086)+chr(1084)+chr(1083)+chr(1077)+chr(1085)+chr(1080)+chr(1103)+" "+chr(1074)+chr(1082)+chr(1083)+chr(1102)+chr(1095)+chr(1077)+chr(1085)+chr(1099)+"!"
    )


@router.message(Command("submit"))
async def cmd_submit(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            chr(1059)+chr(1082)+chr(1072)+chr(1078)+chr(1080)+chr(1090)+chr(1077)+" "+chr(1089)+chr(1089)+chr(1099)+chr(1083)+chr(1082)+chr(1091)+" "
            +chr(1085)+chr(1072)+" "+chr(1089)+chr(1090)+chr(1088)+chr(1080)+chr(1084)+chr(1077)+chr(1086)+chr(1084)+":\n"
            "/submit https://betboom.ru/freestream/nickname"
        )
        return

    url = parts[1].strip()
    if not url.startswith("http"):
        await message.answer(
            chr(1057)+chr(1089)+chr(1099)+chr(1083)+chr(1082)+chr(1072)+" "+chr(1076)+chr(1086)+chr(1083)+chr(1078)+chr(1085)+chr(1072)+" "+chr(1085)+chr(1072)+chr(1095)+chr(1080)+chr(1085)+chr(1072)+chr(1090)+chr(1100)+chr(1089)+chr(1103)+" "+chr(1089)+" http/https."
        )
        return

    # Strip query params and fragments
    clean_url = url.split("?")[0].split("#")[0]
    nickname = clean_url.rstrip("/").split("/")[-1]
    if not nickname:
        await message.answer(
            chr(1053)+chr(1077)+" "+chr(1091)+chr(1076)+chr(1072)+chr(1083)+chr(1086)+chr(1089)+chr(1100)+" "+chr(1080)+chr(1079)+chr(1074)+chr(1083)+chr(1077)+chr(1095)+chr(1100)+" "+chr(1085)+chr(1080)+chr(1082)+chr(1085)+chr(1077)+chr(1081)+chr(1084)+" "+chr(1080)+chr(1079)+" URL."
        )
        return

    if nickname in STREAMERS:
        await message.answer(
            f"\u0421\u0442\u0440\u0438\u043c\u0435\u0440 {nickname} "
            +chr(1091)+chr(1078)+chr(1077)+" "+chr(1086)+chr(1090)+chr(1089)+chr(1083)+chr(1077)+chr(1078)+chr(1080)+chr(1074)+chr(1072)+chr(1077)+chr(1090)+chr(1089)+chr(1103)+"!"
        )
        return

    # Check if roulette is active - first from cache, then by checking page
    state = monitor._states.get(nickname)
    if not state or not state.get("active"):
        # Not in cache - check the page directly
        try:
            check_url = URL_TEMPLATE.format(nickname)
            res = await monitor.check_streamer(nickname, check_url)
            if res.get("active"):
                state = {"active": True, "timer": res.get("timer")}
            else:
                state = None
        except Exception as e:
            logger.error(f"Failed to check {nickname}: {e}")
            state = None

    if state and state.get("active"):
        add_streamer(nickname)
        timer = state.get("timer", "")
        timer_min = timer_to_minutes_str(timer) if timer else ""
        min_line = ""
        if timer_min:
            min_line = f"\n\u23f1 {chr(1054)+chr(1089)+chr(1090)+chr(1072)+chr(1083)+chr(1086)+chr(1089)+chr(1100)} {timer_min}"
        await message.answer(
            f"\u2705 \u0421\u0442\u0440\u0438\u043c\u0435\u0440 {nickname} "
            +chr(1072)+chr(1074)+chr(1090)+chr(1086)+chr(1084)+chr(1072)+chr(1090)+chr(1080)+chr(1095)+chr(1077)+chr(1089)+chr(1082)+chr(1080)+" "
            +chr(1076)+chr(1086)+chr(1073)+chr(1072)+chr(1074)+chr(1083)+chr(1077)+chr(1085)+" "
            +chr(1074)+" "+chr(1084)+chr(1086)+chr(1085)+chr(1080)+chr(1090)+chr(1086)+chr(1088)+chr(1080)+chr(1085)+chr(1075)+"!"
            f"\n{URL_TEMPLATE.format(nickname)}"
            f"{min_line}\n\n"
            +chr(1056)+chr(1091)+chr(1083)+chr(1077)+chr(1090)+chr(1082)+chr(1072)+" "
            +chr(1079)+chr(1072)+chr(1087)+chr(1091)+chr(1097)+chr(1077)+chr(1085)+chr(1072)+", "
            +chr(1091)+chr(1089)+chr(1087)+chr(1077)+chr(1081)+chr(1090)+chr(1077)+" "
            +chr(1087)+chr(1086)+chr(1091)+chr(1095)+chr(1072)+chr(1089)+chr(1090)+chr(1074)+chr(1086)+chr(1074)+chr(1072)+chr(1090)+chr(1100)+"!",
            disable_web_page_preview=True
        )
        # Notify all users about the active roulette
        notify_text = (
            "🎰 "
            +chr(1056)+chr(1091)+chr(1083)+chr(1077)+chr(1090)+chr(1082)+chr(1072)+" "
            +chr(1047)+chr(1040)+chr(1055)+chr(1059)+chr(1065)+chr(1045)+chr(1053)+chr(1040)+"!\n\n"
            f"\u0421\u0442\u0440\u0438\u043c\u0435\u0440: {nickname}\n"
            f"\u0421\u0441\u044b\u043b\u043a\u0430: {URL_TEMPLATE.format(nickname)}{min_line}\n\n"
            "\u0423\u0441\u043f\u0435\u0439 \u043f\u043e\u0443\u0447\u0430\u0441\u0442\u0432\u043e\u0432\u0430\u0442\u044c!"
        )
        await notify_all(message.bot, notify_text)
        return

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
        admin_id = ADMIN_CHAT_ID

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="\u2705 "
                    +chr(1054)+chr(1076)+chr(1086)+chr(1073)+chr(1088)+chr(1080)+chr(1090)+chr(1100),
                    callback_data=f"{CB_APPROVE}{nickname}",
                ),
                InlineKeyboardButton(
                    text="\u274c "
                    +chr(1054)+chr(1090)+chr(1082)+chr(1083)+chr(1086)+chr(1085)+chr(1080)+chr(1090)+chr(1100),
                    callback_data=f"{CB_REJECT}{nickname}",
                ),
            ]
        ])

        await message.bot.send_message(
            chat_id=admin_id,
            text=(
                "\U0001f4e5 "
                +chr(1053)+chr(1086)+chr(1074)+chr(1072)+chr(1103)+" "+chr(1079)+chr(1072)+chr(1103)+chr(1074)+chr(1082)+chr(1072)+"!\n\n"
                f"\u0421\u0442\u0440\u0438\u043c\u0435\u0440: {nickname}\n"
                f"\u0421\u0441\u044b\u043b\u043a\u0430: {url}\n"
                f"\u041e\u0442 @{user_name} (ID: {message.from_user.id})"
            ),
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await message.answer(
            chr(1047)+chr(1072)+chr(1103)+chr(1074)+chr(1082)+chr(1072)+" "+chr(1086)+chr(1090)+chr(1087)+chr(1088)+chr(1072)+chr(1074)+chr(1083)+chr(1077)+chr(1085)+chr(1072)+" "
            +chr(1072)+chr(1076)+chr(1084)+chr(1080)+chr(1085)+chr(1091)+" "+chr(1085)+chr(1072)+" "
            +chr(1086)+chr(1076)+chr(1086)+chr(1073)+chr(1088)+chr(1077)+chr(1085)+chr(1080)+chr(1077)+"."
        )
    except Exception as e:
        logger.error(f"Failed to send to admin: {e}")
        await message.answer(
            chr(1053)+chr(1077)+" "+chr(1091)+chr(1076)+chr(1072)+chr(1083)+chr(1086)+chr(1089)+chr(1100)+" "+chr(1086)+chr(1090)+chr(1087)+chr(1088)+chr(1072)+chr(1074)+chr(1080)+chr(1090)+chr(1100)+" "+chr(1079)+chr(1072)+chr(1103)+chr(1074)+chr(1082)+chr(1091)+" "
            +chr(1072)+chr(1076)+chr(1084)+chr(1080)+chr(1085)+chr(1091)+". "
            +chr(1055)+chr(1086)+chr(1087)+chr(1088)+chr(1086)+chr(1073)+chr(1091)+chr(1081)+chr(1090)+chr(1077)+" "+chr(1087)+chr(1086)+chr(1078)+chr(1072)+chr(1083)+chr(1091)+chr(1081)+chr(1089)+chr(1090)+chr(1072)+"."
        )


@router.callback_query(lambda c: c.data.startswith(CB_APPROVE) or c.data.startswith(CB_REJECT))
async def handle_submit_callback(callback: CallbackQuery):
    data = callback.data
    nickname = data.split(":", 1)[1]

    if nickname not in _pending:
        await callback.answer(
            chr(1047)+chr(1072)+chr(1103)+chr(1074)+chr(1082)+chr(1072)+" "+chr(1085)+chr(1077)+" "+chr(1085)+chr(1072)+chr(1081)+chr(1076)+chr(1077)+chr(1085)+chr(1072)+" "
            +chr(1080)+chr(1083)+chr(1080)+" "+chr(1091)+chr(1089)+chr(1090)+chr(1072)+chr(1088)+chr(1077)+chr(1083)+chr(1072)+".",
            show_alert=True,
        )
        return

    sub = _pending.pop(nickname)
    user_id = sub["user_id"]
    chat_id = sub["chat_id"]

    is_approve = data.startswith(CB_APPROVE)

    if is_approve:
        add_streamer(nickname)

        approve_text = chr(1054)+chr(1076)+chr(1086)+chr(1073)+chr(1088)+chr(1077)+chr(1085)+chr(1086)+"!"
        await callback.message.edit_text(
            callback.message.text + f"\n\n\u2705 {approve_text}"
        )
        try:
            await callback.bot.send_message(
                chat_id,
                f"\u2705 \u0421\u0442\u0440\u0438\u043c\u0435\u0440 {nickname} "
                +chr(1086)+chr(1076)+chr(1086)+chr(1073)+chr(1088)+chr(1077)+chr(1085)+" "
                +chr(1080)+" "+chr(1076)+chr(1086)+chr(1073)+chr(1072)+chr(1074)+chr(1083)+chr(1077)+chr(1085)+" "
                +chr(1074)+" "+chr(1084)+chr(1086)+chr(1085)+chr(1080)+chr(1090)+chr(1086)+chr(1088)+chr(1080)+chr(1085)+chr(1075)+"!",
            )
        except Exception:
            pass
    else:
        reject_text = chr(1054)+chr(1090)+chr(1082)+chr(1083)+chr(1086)+chr(1085)+chr(1077)+chr(1085)+chr(1086)+"."
        await callback.message.edit_text(
            callback.message.text + f"\n\n\u274c {reject_text}"
        )
        try:
            await callback.bot.send_message(
                chat_id,
                f"\u274c \u0421\u0442\u0440\u0438\u043c\u0435\u0440 {nickname} "
                +chr(1086)+chr(1090)+chr(1082)+chr(1083)+chr(1086)+chr(1085)+chr(1077)+chr(1085)+" "
                +chr(1072)+chr(1076)+chr(1084)+chr(1080)+chr(1085)+chr(1086)+chr(1084)+".",
            )
        except Exception:
            pass

    await callback.answer()


async def monitoring_loop(bot: Bot):
    CONCURRENT_CHECKS = 5

    while True:
        if not chat_ids:
            await asyncio.sleep(5)
            continue

        cycle_start = time.monotonic()
        current_streamers = list(STREAMERS)

        for i in range(0, len(current_streamers), CONCURRENT_CHECKS):
            batch = current_streamers[i : i + CONCURRENT_CHECKS]

            async def check_one(nickname):
                url = URL_TEMPLATE.format(nickname)
                return nickname, url, await monitor.check_streamer(nickname, url)

            results = await asyncio.gather(
                *(check_one(n) for n in batch), return_exceptions=True
            )

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Check failed: {result}")
                    continue

                nickname, url, res = result

                if res.get("error"):
                    logger.error(f"Error checking {nickname}: {res['error']}")
                    continue

                if (res.get("changed") or res.get("first_check")) and res.get("active"):
                    timer = res.get("timer", "")
                    timer_min = timer_to_minutes_str(timer) if timer else ""
                    min_line = ""
                    if timer_min:
                        min_line = f"\n\u23f1 {chr(1054)+chr(1089)+chr(1090)+chr(1072)+chr(1083)+chr(1086)+chr(1089)+chr(1100)} {timer_min}"
                    text = (
                        "\U0001f3b0 "
                        +chr(1056)+chr(1059)+chr(1051)+chr(1045)+chr(1058)+chr(1050)+chr(1040)+" "
                        +chr(1047)+chr(1040)+chr(1055)+chr(1059)+chr(1065)+chr(1045)+chr(1053)+chr(1040)+"!\n\n"
                        f"\u0421\u0442\u0440\u0438\u043c\u0435\u0440: {nickname}\n"
                        f"\u0421\u0441\u044b\u043b\u043a\u0430: {url}{min_line}\n\n"
                        "\u0423\u0441\u043f\u0435\u0439 \u043f\u043e\u0443\u0447\u0430\u0441\u0442\u0432\u043e\u0432\u0430\u0442\u044c!"
                    )
                    await notify_all(bot, text)
                    logger.info(f"Notification sent for {nickname}")



        # Persist states after each cycle
        save_roulette_states(monitor._states)
        last_cycle_time["time"] = datetime.now().strftime("%H:%M:%S")

        elapsed = time.monotonic() - cycle_start
        remaining = max(0, CHECK_INTERVAL - elapsed)
        active_count = sum(1 for v in monitor._states.values() if v.get("active"))
        logger.info(
            f"Cycle: {active_count} active, {len(STREAMERS)} checked, "
            f"{elapsed:.0f}s, next in {remaining:.0f}s"
        )
        await asyncio.sleep(remaining)


async def main():
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=TELEGRAM_TOKEN, session=session)

    # Load saved states so first_check doesn't re-notify
    saved_states = load_roulette_states()
    if saved_states:
        monitor._states.update(saved_states)
        logger.info(f"Loaded {len(saved_states)} saved roulette states")

    await monitor.start()
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u0431\u043e\u0442\u0430"),
        BotCommand(command="status", description="\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u0440\u0443\u043b\u0435\u0442\u043a\u0438"),
        BotCommand(command="list", description="\u0421\u043f\u0438\u0441\u043e\u043a \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u043e\u0432"),
        BotCommand(command="submit", description="\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0438\u0442\u044c \u0441\u0442\u0440\u0438\u043c\u0435\u0440\u0430"),
        BotCommand(command="mute", description="\u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f"),
        BotCommand(command="unmute", description="\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f"),
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
