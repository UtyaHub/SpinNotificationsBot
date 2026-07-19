import asyncio
import logging
import re
from pathlib import Path
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
DEBUG_DIR = Path(__file__).parent / "debug"


class StreamMonitor:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._states = {}
        self._initialized = False

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._initialized = True
        logger.info("Playwright browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Playwright browser stopped")

    async def check_streamer(self, nickname, url) -> dict:
        page = await self._browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(8000)
            content = await page.content()
            if nickname not in self._states:
                DEBUG_DIR.mkdir(exist_ok=True)
                debug_file = DEBUG_DIR / f"{nickname}_first_check.html"
                debug_file.write_text(content, encoding="utf-8")
                logger.info(f"[{nickname}] Debug HTML saved ({len(content)} bytes)")
            is_active = self._detect_roulette_activity(content)
            timer_text = self._extract_timer(content) if is_active else None
            prev_state = self._states.get(nickname)
            prev_active = prev_state["active"] if prev_state else None
            self._states[nickname] = {"active": is_active, "timer": timer_text}
            if prev_active is None:
                logger.info(f"[{nickname}] {'ACTIVE' if is_active else 'inactive'}")
                return {"changed": False, "active": is_active, "first_check": True, "timer": timer_text}
            changed = prev_active != is_active
            status = "ACTIVE" if is_active else "inactive"
            if changed:
                logger.info(f"[{nickname}] CHANGED -> {status}")
            else:
                logger.debug(f"[{nickname}] {status}")
            return {"changed": changed, "active": is_active, "first_check": False, "timer": timer_text}
        except Exception as e:
            logger.error(f"[{nickname}] Error checking page: {e}")
            return {"changed": False, "active": None, "error": str(e)}
        finally:
            await page.close()

    def _detect_roulette_activity(self, html: str) -> bool:
        html_lower = html.lower()
        if 'пока ждёшь следующий запуск' in html_lower:
            return False
        if 'до розыгрыша' in html_lower:
            return True
        inactive_signals = ['завершён', 'закончен', 'розыгрыш окончен', "ended", "finished", "completed", 'результаты розыгрыша']
        if any(sig in html_lower for sig in inactive_signals):
            return False
        return False

    def _extract_timer(self, html: str) -> str | None:
        pattern = 'до розыгрыша' + r'[^0-9]*(\d{2}:\d{2}:\d{2})'
        match = re.search(pattern, html.lower())
        if match:
            return match.group(1)
        return None
