"""
Shared browser factory with stealth mode and realistic headers.
All extractors use this to avoid bot detection.
"""
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]

_VIEWPORT = {"width": 1366, "height": 768}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def stealth_page(playwright) -> tuple[Browser, BrowserContext, Page]:
    """Return (browser, context, page) with stealth settings applied."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=_LAUNCH_ARGS,
    )
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=_VIEWPORT,
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        extra_http_headers={
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    # Remove navigator.webdriver fingerprint
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR','tr','en-US']});
        window.chrome = {runtime: {}};
    """)
    page = await context.new_page()
    return browser, context, page
