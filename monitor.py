import asyncio
import logging
from datetime import datetime, timezone, timedelta
import aiohttp

logger = logging.getLogger(__name__)

API_URL = "https://betboom.ru/api/streamer-wheel/action/get-info"
HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "x-platform": "web",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
MAX_RETRIES = 2
RETRY_DELAY = 2


class StreamMonitor:
    def __init__(self):
        self._session = None
        self._states = {}
        self._initialized = False

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=15)
        self._session = aiohttp.ClientSession(headers=HEADERS, timeout=timeout)
        self._initialized = True
        logger.info("HTTP session started (no browser)")

    async def stop(self):
        if self._session:
            await self._session.close()
        logger.info("HTTP session stopped")

    async def check_streamer(self, nickname, url) -> dict:
        payload = {"streamer_link": url}
        last_err = None

        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.post(API_URL, json=payload) as resp:
                    if resp.status == 503:
                        last_err = f"503 from server"
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        return {"changed": False, "active": None, "error": last_err}

                    if resp.status != 200:
                        return {"changed": False, "active": None, "error": f"HTTP {resp.status}"}

                    ct = resp.headers.get("Content-Type", "")
                    if "json" not in ct:
                        text = await resp.text()
                        last_err = f"Non-JSON response ({resp.status})"
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        return {"changed": False, "active": None, "error": last_err}

                    data = await resp.json()

                if data.get("code") != 200:
                    return {"changed": False, "active": None, "error": f"API code {data.get('code')}"}

                info = data.get("info", {})
                is_ended = info.get("is_ended", True)

                is_active = False
                timer_text = None
                if not is_ended:
                    start_dttm = info.get("start_dttm")
                    duration_min = info.get("duration_min")
                    if start_dttm and duration_min:
                        try:
                            start = datetime.fromisoformat(start_dttm.replace("Z", "+00:00"))
                            end = start + timedelta(minutes=duration_min)
                            now = datetime.now(timezone.utc)
                            is_active = start <= now <= end
                            if is_active:
                                remaining = (end - now).total_seconds()
                                h = int(remaining // 3600)
                                m = int((remaining % 3600) // 60)
                                s = int(remaining % 60)
                                timer_text = f"{h:02d}:{m:02d}:{s:02d}"
                        except Exception:
                            is_active = False

                prev_state = self._states.get(nickname)
                prev_active = prev_state["active"] if prev_state else None

                self._states[nickname] = {"active": is_active, "timer": timer_text}

                if prev_active is None:
                    logger.info(f"[{nickname}] {'ACTIVE' if is_active else 'inactive'}")
                    return {"changed": False, "active": is_active, "first_check": True, "timer": timer_text}

                changed = prev_active != is_active
                if changed:
                    logger.info(f"[{nickname}] CHANGED -> {'ACTIVE' if is_active else 'inactive'}")
                else:
                    logger.debug(f"[{nickname}] {'ACTIVE' if is_active else 'inactive'}")
                return {"changed": changed, "active": is_active, "first_check": False, "timer": timer_text}

            except asyncio.TimeoutError:
                last_err = "Timeout"
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"changed": False, "active": None, "error": last_err}
            except Exception as e:
                last_err = str(e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"changed": False, "active": None, "error": last_err}

        return {"changed": False, "active": None, "error": last_err or "Unknown error"}