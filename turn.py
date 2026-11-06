#!/usr/bin/env python3
"""Turnstile Solver - single-file bundle.

Modes
-----
  python turn.py serve [options]                    # HTTP API (default)
  python turn.py solve --url URL --sitekey KEY      # one-off, synchronous
  python turn.py solve --url URL --sitekey KEY --async

Only use this against sites you own or are authorised to test. For a harmless
smoke test use Cloudflare's published dummy sitekeys, e.g. 1x00000000000000000000AA
(always passes).
"""
import os
import sys
import time
import uuid
import json
import html
import random
import asyncio
import logging
import argparse
from dataclasses import dataclass, asdict
from typing import Optional

BROWSER_TYPES = ["chromium", "chrome", "msedge", "camoufox"]
CHANNEL_BROWSERS = ["chromium", "chrome", "msedge"]  # launched through patchright

COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS['RESET']}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("Turnstile")
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.propagate = False


# --------------------------------------------------------------------------- #
# Shared page template / helpers
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Turnstile Solver</title>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
    <script>
        async function fetchIP() {
            try {
                const response = await fetch('https://api64.ipify.org?format=json');
                const data = await response.json();
                document.getElementById('ip-display').innerText = `Your IP: ${data.ip}`;
            } catch (error) {
                console.error('Error fetching IP:', error);
                document.getElementById('ip-display').innerText = 'Failed to fetch IP';
            }
        }
        window.onload = fetchIP;
    </script>
</head>
<body>
    <!-- cf turnstile -->
    <p id="ip-display">Fetching your IP...</p>
</body>
</html>
"""

WIDGET_SELECTOR = "//div[@class='cf-turnstile']"
RESPONSE_SELECTOR = "[name=cf-turnstile-response]"


@dataclass
class TurnstileResult:
    turnstile_value: Optional[str]
    elapsed_time_seconds: float
    status: str
    reason: Optional[str] = None


def with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def build_page(template: str, sitekey: str, action: str = None, cdata: str = None, style: str = "") -> str:
    """Render the page with the Turnstile div. Attribute values are HTML-escaped."""
    attrs = f'class="cf-turnstile"'
    if style:
        attrs += f' style="{style}"'
    attrs += f' data-sitekey="{html.escape(sitekey, quote=True)}"'
    if action:
        attrs += f' data-action="{html.escape(action, quote=True)}"'
    if cdata:
        attrs += f' data-cdata="{html.escape(cdata, quote=True)}"'
    return template.replace("<!-- cf turnstile -->", f"<div {attrs}></div>")


def validate_options(browser_type: str, headless: bool, useragent: Optional[str]) -> Optional[str]:
    """Return an error message, or None if the options are valid."""
    if browser_type not in BROWSER_TYPES:
        return f"Unknown browser type: {browser_type}. Available browser types: {BROWSER_TYPES}"
    if headless and useragent is None and browser_type != "camoufox":
        return "Headless mode needs --useragent (or use --browser_type camoufox)."
    return None


def failure(start: float, reason: str) -> TurnstileResult:
    return TurnstileResult(None, round(time.time() - start, 3), "failure", reason)


# --------------------------------------------------------------------------- #
# Sync solver
# --------------------------------------------------------------------------- #
class TurnstileSolver:
    HTML_TEMPLATE = HTML_TEMPLATE

    def __init__(self, debug=False, headless=False, useragent=None, browser_type="chromium"):
        self.debug = debug
        self.headless = headless
        self.useragent = useragent
        self.browser_type = browser_type
        self.browser_args = [f"--user-agent={useragent}"] if useragent else []

    def _get_response(self, page, max_attempts: int = 10) -> Optional[str]:
        for attempt in range(1, max_attempts + 1):
            try:
                value = page.input_value(RESPONSE_SELECTOR, timeout=2000)
                if value:
                    return value
                if self.debug:
                    logger.debug(f"Attempt {attempt}: no Turnstile response yet.")
                page.click(WIDGET_SELECTOR, timeout=3000)
                time.sleep(0.5)
            except Exception:
                pass
        return None

    def solve(self, url, sitekey, action=None, cdata=None) -> TurnstileResult:
        from patchright.sync_api import sync_playwright

        start = time.time()
        playwright = browser = None
        try:
            if self.browser_type == "camoufox":
                from camoufox.sync_api import Camoufox
                browser = Camoufox(headless=self.headless).start()
            else:
                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(
                    channel=self.browser_type, headless=self.headless, args=self.browser_args)

            page = browser.new_page()
            target = with_slash(url)
            body = build_page(self.HTML_TEMPLATE, sitekey, action, cdata)
            if self.debug:
                logger.debug(f"Navigating to {target}")
            page.route(target, lambda route: route.fulfill(body=body, status=200))
            page.goto(target)

            value = self._get_response(page)
            if value:
                elapsed = round(time.time() - start, 3)
                logger.success(f"Solved captcha: {value[:45]}... in {elapsed} seconds")
                return TurnstileResult(value, elapsed, "success")
            logger.error("Failed to retrieve Turnstile value.")
            return failure(start, "Max attempts reached without token retrieval")
        except Exception as e:
            logger.error(f"Solver error: {e}")
            return failure(start, str(e))
        finally:
            for closer in (lambda: browser and browser.close(), lambda: playwright and playwright.stop()):
                try:
                    closer()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Async solver
# --------------------------------------------------------------------------- #
class AsyncTurnstileSolver:
    HTML_TEMPLATE = HTML_TEMPLATE

    def __init__(self, debug=False, headless=False, useragent=None, browser_type="chromium"):
        self.debug = debug
        self.headless = headless
        self.useragent = useragent
        self.browser_type = browser_type
        self.browser_args = [f"--user-agent={useragent}"] if useragent else []

    async def _get_response(self, page, max_attempts: int = 10) -> Optional[str]:
        for attempt in range(1, max_attempts + 1):
            try:
                value = await page.input_value(RESPONSE_SELECTOR, timeout=2000)
                if value:
                    return value
                if self.debug:
                    logger.debug(f"Attempt {attempt}: no Turnstile response yet.")
                await page.click(WIDGET_SELECTOR, timeout=3000)
                await asyncio.sleep(0.5)
            except Exception:
                pass
        return None

    async def solve(self, url, sitekey, action=None, cdata=None) -> TurnstileResult:
        from patchright.async_api import async_playwright

        start = time.time()
        playwright = browser = None
        try:
            if self.browser_type == "camoufox":
                from camoufox.async_api import AsyncCamoufox
                browser = await AsyncCamoufox(headless=self.headless).start()
            else:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    channel=self.browser_type, headless=self.headless, args=self.browser_args)

            page = await browser.new_page()
            target = with_slash(url)
            body = build_page(self.HTML_TEMPLATE, sitekey, action, cdata)
            if self.debug:
                logger.debug(f"Navigating to {target}")
            await page.route(target, lambda route: route.fulfill(body=body, status=200))
            await page.goto(target)

            value = await self._get_response(page)
            if value:
                elapsed = round(time.time() - start, 3)
                logger.success(f"Solved captcha: {value[:45]}... in {elapsed} seconds")
                return TurnstileResult(value, elapsed, "success")
            logger.error("Failed to retrieve Turnstile value.")
            return failure(start, "Max attempts reached without token retrieval")
        except Exception as e:
            logger.error(f"Solver error: {e}")
            return failure(start, str(e))
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass


def get_turnstile_token(url, sitekey, action=None, cdata=None, debug=False, headless=False,
                        useragent=None, browser_type="chromium") -> dict:
    err = validate_options(browser_type, headless, useragent)
    if err:
        logger.error(err)
        return asdict(TurnstileResult(None, 0.0, "failure", err))
    return asdict(TurnstileSolver(debug, headless, useragent, browser_type).solve(url, sitekey, action, cdata))


async def get_turnstile_token_async(url, sitekey, action=None, cdata=None, debug=False, headless=False,
                                    useragent=None, browser_type="chromium") -> dict:
    err = validate_options(browser_type, headless, useragent)
    if err:
        logger.error(err)
        return asdict(TurnstileResult(None, 0.0, "failure", err))
    return asdict(await AsyncTurnstileSolver(debug, headless, useragent, browser_type).solve(
        url, sitekey, action, cdata))


# --------------------------------------------------------------------------- #
# API server
# --------------------------------------------------------------------------- #
INDEX_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Turnstile Solver API</title>
    <style>
        body { background:#111827; color:#e5e7eb; font-family:system-ui,sans-serif; display:flex;
               align-items:center; justify-content:center; min-height:100vh; margin:0; }
        .card { background:#1f2937; border:1px solid #ef4444; border-radius:8px; padding:2rem; max-width:40rem; }
        h1 { color:#ef4444; text-align:center; } code { background:#7f1d1d; padding:2px 6px; border-radius:4px; }
    </style>
</head>
<body><div class="card">
    <h1>Turnstile Solver API</h1>
    <p>Send a GET request to <code>/turnstile</code> with <code>url</code> and <code>sitekey</code>
       (optional: <code>action</code>, <code>cdata</code>), then poll <code>/result?id=TASK_ID</code>.</p>
    <p><code>/turnstile?url=https://example.com&amp;sitekey=SITEKEY</code></p>
</div></body></html>
"""


class TurnstileAPIServer:
    HTML_TEMPLATE = HTML_TEMPLATE

    def __init__(self, headless, useragent, debug, browser_type, thread, proxy_support):
        from quart import Quart
        self.app = Quart(__name__)
        self.debug = debug
        self.results = self._load_results()
        self.browser_type = browser_type
        self.headless = headless
        self.useragent = useragent
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_args = [f"--user-agent={useragent}"] if useragent else []
        self._playwright = None
        self._camoufox = None
        self._setup_routes()

    @staticmethod
    def _load_results():
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading results: {e}. Starting with an empty results dictionary.")
        return {}

    def _save_results(self):
        try:
            with open("results.json", "w") as f:
                json.dump(self.results, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving results to file: {e}")

    def _setup_routes(self):
        self.app.before_serving(self._startup)
        self.app.after_serving(self._shutdown)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/')(self.index)

    async def _startup(self):
        logger.info("Starting browser initialization")
        try:
            if self.browser_type in CHANNEL_BROWSERS:
                from patchright.async_api import async_playwright
                self._playwright = await async_playwright().start()
            else:
                from camoufox.async_api import AsyncCamoufox
                self._camoufox = AsyncCamoufox(headless=self.headless)

            for i in range(1, self.thread_count + 1):
                if self._playwright:
                    browser = await self._playwright.chromium.launch(
                        channel=self.browser_type, headless=self.headless, args=self.browser_args)
                else:
                    browser = await self._camoufox.start()
                await self.browser_pool.put((i, browser))
                if self.debug:
                    logger.success(f"Browser {i} initialized successfully")
            logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            raise

    async def _shutdown(self):
        while not self.browser_pool.empty():
            _, browser = self.browser_pool.get_nowait()
            try:
                await browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    def _pick_proxy(self):
        """Return kwargs for browser.new_context() or {} when no proxy applies."""
        if not self.proxy_support:
            return None, {}
        with open(os.path.join(os.getcwd(), "proxies.txt")) as f:
            proxies = [line.strip() for line in f if line.strip() and not line.startswith("protocol://")]
        if not proxies:
            return None, {}
        proxy = random.choice(proxies)
        parts = proxy.split(':')
        if len(parts) == 3:
            return proxy, {"proxy": {"server": proxy}}
        if len(parts) == 5:
            scheme, ip, port, user, password = parts
            return proxy, {"proxy": {"server": f"{scheme}://{ip}:{port}", "username": user, "password": password}}
        raise ValueError("Invalid proxy format")

    async def _solve_turnstile(self, task_id, url, sitekey, action=None, cdata=None):
        index, browser = await self.browser_pool.get()
        start = time.time()
        context = None
        try:
            proxy, ctx_kwargs = self._pick_proxy()
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            if self.debug:
                logger.debug(f"Browser {index}: solving {url} sitekey={sitekey} proxy={proxy}")

            target = with_slash(url)
            body = build_page(self.HTML_TEMPLATE, sitekey, action, cdata, style="background: white;")
            await page.route(target, lambda route: route.fulfill(body=body, status=200))
            await page.goto(target)
            await page.eval_on_selector(WIDGET_SELECTOR, "el => el.style.width = '70px'")

            for attempt in range(10):
                try:
                    value = await page.input_value(RESPONSE_SELECTOR, timeout=2000)
                    if value:
                        elapsed = round(time.time() - start, 3)
                        logger.success(f"Browser {index}: solved captcha - {COLORS['MAGENTA']}{value[:10]}"
                                       f"{COLORS['RESET']} in {COLORS['GREEN']}{elapsed}{COLORS['RESET']} seconds")
                        self.results[task_id] = {"value": value, "elapsed_time": elapsed}
                        self._save_results()
                        break
                    if self.debug:
                        logger.debug(f"Browser {index}: attempt {attempt} - no response yet")
                    await page.locator(WIDGET_SELECTOR).click(timeout=1000)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": round(time.time() - start, 3)}
                if self.debug:
                    logger.error(f"Browser {index}: failed to solve Turnstile")
        except Exception as e:
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": round(time.time() - start, 3)}
            logger.error(f"Browser {index}: error solving Turnstile: {e}")
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        from quart import request, jsonify
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        if not url or not sitekey:
            return jsonify({"status": "error", "error": "Both 'url' and 'sitekey' are required"}), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"
        asyncio.create_task(self._solve_turnstile(
            task_id, url, sitekey, request.args.get('action'), request.args.get('cdata')))
        if self.debug:
            logger.debug(f"Request accepted with task id {task_id}.")
        return jsonify({"task_id": task_id}), 202

    async def get_result(self):
        from quart import request, jsonify
        task_id = request.args.get('id')
        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        # Fixed: previously `"CAPTCHA_FAIL" in result` tested dict *keys*, so failures returned 200.
        failed = isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL"
        return (jsonify(result) if isinstance(result, dict) else result), (422 if failed else 200)

    @staticmethod
    async def index():
        return INDEX_PAGE


def create_app(headless=False, useragent=None, debug=False, browser_type="chromium", thread=1, proxy_support=False):
    return TurnstileAPIServer(headless, useragent, debug, browser_type, thread, proxy_support).app


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="Turnstile solver (single-file bundle)")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument('--headless', action='store_true', help='Run headless (needs --useragent unless camoufox)')
        sp.add_argument('--useragent', default=None, help='Custom User-Agent string')
        sp.add_argument('--debug', action='store_true', help='Verbose logging')
        sp.add_argument('--browser_type', default='chromium', choices=BROWSER_TYPES)

    s = sub.add_parser('serve', help='Run the HTTP API (default)')
    common(s)
    s.add_argument('--thread', type=int, default=1, help='Number of browsers in the pool')
    s.add_argument('--proxy', action='store_true', help='Pick a random proxy from proxies.txt')
    s.add_argument('--host', default='127.0.0.1')
    s.add_argument('--port', type=int, default=5000)

    o = sub.add_parser('solve', help='Solve once and print the result as JSON')
    common(o)
    o.add_argument('--url', required=True)
    o.add_argument('--sitekey', required=True)
    o.add_argument('--action', default=None)
    o.add_argument('--cdata', default=None)
    o.add_argument('--async', dest='use_async', action='store_true', help='Use the async solver')
    return p


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('serve', 'solve', '-h', '--help'):
        argv.insert(0, 'serve')  # `serve` is the default command
    args = parser.parse_args(argv)

    err = validate_options(args.browser_type, args.headless, args.useragent)
    if err:
        logger.error(err)
        return 2

    if args.command == 'solve':
        kwargs = dict(url=args.url, sitekey=args.sitekey, action=args.action, cdata=args.cdata,
                      debug=args.debug, headless=args.headless, useragent=args.useragent,
                      browser_type=args.browser_type)
        result = asyncio.run(get_turnstile_token_async(**kwargs)) if args.use_async else get_turnstile_token(**kwargs)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "success" else 1

    app = create_app(headless=args.headless, useragent=args.useragent, debug=args.debug,
                     browser_type=args.browser_type, thread=args.thread, proxy_support=args.proxy)
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
