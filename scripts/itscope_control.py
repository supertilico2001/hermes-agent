#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from playwright.async_api import async_playwright, BrowserContext, Page


HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
ITSCOPE_HOME = HERMES_HOME / "sessions" / "itscope"
PROFILE_DIR = ITSCOPE_HOME / "playwright-profile"
ARTIFACT_DIR = ITSCOPE_HOME / "artifacts"
STATE_FILE = ITSCOPE_HOME / "state.json"
STORAGE_STATE_FILE = PROFILE_DIR / "storage_state.json"
DEFAULT_HOME_URL = "https://www.itscope.com/red/app#home/portal/-/-"
SEARCH_URL_TEMPLATE = "https://www.itscope.com/red/app#products/search/{query}/-"
LOGIN_MARKERS = (
    "one exact identity",
    "sign in with your email address",
    "forgot password?",
    "email address",
    "password",
)
PRODUCT_RESULT_ROUTE_MARKERS = (
    "#products/",
    "#/products/",
    "/red/app#products/",
)


@dataclass
class SessionState:
    logged_in: bool = False
    last_url: str = ""
    last_title: str = ""
    last_action: str = ""
    last_query: str = ""
    screenshot_path: str = ""
    note: str = ""


@dataclass
class Row:
    product: str = ""
    manufacturer: str = ""
    reference: str = ""
    provider: str = ""
    stock: str = ""
    price: str = ""
    availability: str = ""


def ensure_dirs() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ITSCOPE_HOME.mkdir(parents=True, exist_ok=True)


def save_state(state: SessionState) -> None:
    ensure_dirs()
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")


def load_state() -> SessionState:
    if not STATE_FILE.exists():
        return SessionState()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        defaults = asdict(SessionState())
        defaults.update({k: data.get(k, defaults.get(k, "")) for k in SessionState.__annotations__})
        return SessionState(**defaults)
    except Exception:
        return SessionState(note="Could not parse saved state")

def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def text_contains_any(text: str, needles: Iterable[str]) -> bool:
    lower = text.lower()
    return any(n.lower() in lower for n in needles)


def parse_price(text: str) -> float | None:
    if not text:
        return None
    text = normalize_text(text)
    m = re.search(r"(?:€|eur|eur\.)\s*([\d.,]+)", text, re.I)
    if not m:
        m = re.search(r"([\d][\d.,]*\d|\d)\s*(?:€|eur)\b", text, re.I)
    if not m:
        m = re.search(r"([\d][\d.,]*\d|\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)", text)
    if not m:
        return None
    raw = m.group(1)
    if raw.count(",") and raw.count("."):
        # Assume thousand separators are dots and decimals are commas.
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(","):
        # If there is only one comma and 1-2 digits after it, treat as decimal.
        if re.search(r",\d{1,2}$", raw):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    else:
        # Dot may be decimal or thousand separator; try to infer from suffix.
        if re.search(r"\.\d{1,2}$", raw):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except Exception:
        return None


def format_price(value: float | None) -> str:
    if value is None:
        return ""
    return f"€{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_stock(text: str) -> str:
    if not text:
        return ""
    t = normalize_text(text)
    if re.search(r"\b(in stock|available|available now|stock)\b", t, re.I):
        m = re.search(r"\b(\d+)\b", t)
        if m:
            return m.group(1)
        return t
    m = re.search(r"\b(\d+)\b", t)
    return m.group(1) if m else t


async def with_playwright(headless: bool):
    p = await async_playwright().start()
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1600, "height": 1100},
            locale="en-GB",
            timezone_id="Europe/Madrid",
            accept_downloads=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            yield p, context
        finally:
            await context.close()
    finally:
        await p.stop()


class PWSession:
    def __init__(self, headless: bool):
        self.headless = headless
        self._manager = None
        self.p = None
        self.context: BrowserContext | None = None

    async def __aenter__(self):
        self._manager = with_playwright(self.headless)
        self.p, self.context = await self._manager.__anext__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.context is not None:
            await self.context.close()
        if self.p is not None:
            await self.p.stop()

    async def new_page(self) -> Page:
        assert self.context is not None
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return page


async def open_page(headless: bool) -> tuple[Any, BrowserContext, Page]:
    p = await async_playwright().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1600, "height": 1100},
        locale="en-GB",
        timezone_id="Europe/Madrid",
        accept_downloads=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return p, context, page


async def close_page(p, context):
    try:
        await context.close()
    finally:
        await p.stop()


async def body_text(page: Page) -> str:
    try:
        return normalize_text(await page.locator("body").inner_text(timeout=5000))
    except Exception:
        return ""


async def is_login_page(page: Page) -> bool:
    try:
        if await page.locator("input[type='password']").count():
            return True
    except Exception:
        pass
    text = (await body_text(page)).lower()
    return text_contains_any(text, LOGIN_MARKERS)


async def page_ready(page: Page, url: str):
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")


async def ensure_logged_in(page: Page, interactive_login: bool = False) -> bool:
    await page_ready(page, DEFAULT_HOME_URL)
    if not await is_login_page(page):
        return True

    if not interactive_login:
        return False

    print("ITscope login is required. A browser window should be open now.")
    print("Log in manually, then press Enter here to continue.")
    try:
        input()
    except EOFError:
        pass
    await page.wait_for_timeout(1000)
    await page.goto(DEFAULT_HOME_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    return not await is_login_page(page)


async def try_find_search_input(page: Page):
    candidates = [
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
        "input[type='search']",
        "input[name*='search' i]",
        "input[placeholder*='products' i]",
        "input",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        try:
            if await loc.count():
                return loc.first
        except Exception:
            continue
    return None


async def search_direct(page: Page, query: str):
    q = quote(query, safe="")
    await page_ready(page, SEARCH_URL_TEMPLATE.format(query=q))
    if await is_login_page(page):
        return False
    return True


async def maybe_click_first_product(page: Page, query: str):
    q = query.lower().strip()
    links = page.locator("a[href*='#products/'], a[href*='/red/app#products/']")
    try:
        total = await links.count()
    except Exception:
        total = 0
    if total == 0:
        return False

    def score_text(t: str) -> int:
        tl = t.lower()
        score = 0
        if q and q in tl:
            score += 100
        for token in [tok for tok in re.split(r"\s+", q) if tok]:
            if token in tl:
                score += 10
        if re.search(r"\b5090\b", tl):
            score += 30
        if re.search(r"\brtx\b", tl):
            score += 5
        return score

    best_idx = None
    best_score = -1
    max_scan = min(total, 40)
    for i in range(max_scan):
        try:
            t = normalize_text(await links.nth(i).inner_text(timeout=2000))
        except Exception:
            t = ""
        s = score_text(t)
        if s > best_score:
            best_score = s
            best_idx = i
    if best_idx is None or best_score <= 0:
        return False
    try:
        await links.nth(best_idx).click(timeout=5000)
        await page.wait_for_load_state("networkidle")
        return True
    except Exception:
        return False


async def click_sort_if_present(page: Page, direction: str):
    # Try obvious headers or buttons first.
    needles = ["Price", "price", "Cost", "cost", "Preis", "price per", "unit price"]
    locators = []
    for needle in needles:
        locators.extend([
            page.get_by_text(needle, exact=False),
            page.locator(f"th:has-text('{needle}')"),
            page.locator(f"button:has-text('{needle}')"),
            page.locator(f"[role='button']:has-text('{needle}')"),
        ])
    for loc in locators:
        try:
            if await loc.count():
                # clicking twice often toggles asc/desc; we only need best effort.
                await loc.first.click(timeout=2500)
                await page.wait_for_timeout(300)
                if direction == "price_desc":
                    await loc.first.click(timeout=2500)
                    await page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    return False


async def click_stock_filter_if_present(page: Page):
    needles = ["stock", "availability", "available", "lager", "verfügbarkeit", "in stock"]
    for needle in needles:
        locators = [
            page.get_by_text(needle, exact=False),
            page.locator(f"button:has-text('{needle}')"),
            page.locator(f"label:has-text('{needle}')"),
            page.locator(f"[role='button']:has-text('{needle}')"),
            page.locator(f"th:has-text('{needle}')"),
        ]
        for loc in locators:
            try:
                if await loc.count():
                    await loc.first.click(timeout=2500)
                    await page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
    return False


async def extract_rows_from_tables(page: Page) -> list[Row]:
    out: list[Row] = []
    tables = page.locator("table")
    count = await tables.count()
    for ti in range(min(count, 8)):
        table = tables.nth(ti)
        try:
            headers = [normalize_text(h) for h in await table.locator("thead th, tr:first-child th, tr:first-child td").all_inner_texts()]
        except Exception:
            headers = []
        header_text = " | ".join(headers).lower()
        if not any(k in header_text for k in ("price", "stock", "availability", "provider", "supplier", "manufacturer", "reference", "stock", "price")):
            continue

        row_loc = table.locator("tbody tr")
        row_count = await row_loc.count()
        for ri in range(min(row_count, 30)):
            row = row_loc.nth(ri)
            try:
                cells = [normalize_text(c) for c in await row.locator("th,td").all_inner_texts()]
            except Exception:
                cells = []
            if not cells:
                continue
            row_text = " | ".join(cells)
            if not re.search(r"[€$£]|\b(stock|availability|available|in stock|out of stock)\b", row_text, re.I):
                # Still allow rows if table clearly matches the supplier area.
                if not any(k in header_text for k in ("provider", "supplier", "stock", "price")):
                    continue
            r = Row()
            # Assign by header names if possible.
            mapping = {h.lower(): cells[idx] for idx, h in enumerate(headers) if idx < len(cells)}
            candidate = " ".join(cells)
            if "product" in mapping:
                r.product = mapping["product"]
            if "manufacturer" in mapping:
                r.manufacturer = mapping["manufacturer"]
            if "ref" in mapping or "reference" in mapping:
                r.reference = mapping.get("reference") or mapping.get("ref") or ""
            if "provider" in mapping:
                r.provider = mapping["provider"]
            if "supplier" in mapping and not r.provider:
                r.provider = mapping["supplier"]
            if "stock" in mapping:
                r.stock = parse_stock(mapping["stock"])
            if "availability" in mapping:
                r.availability = mapping["availability"]
            if "price" in mapping:
                price_val = parse_price(mapping["price"])
                r.price = format_price(price_val) or normalize_text(mapping["price"])

            # Fallbacks from row text
            if not r.price:
                price_val = parse_price(candidate)
                if price_val is not None:
                    r.price = format_price(price_val)
            if not r.stock:
                stock_match = re.search(r"(?:stock|availability|lager)[:\s]*([\w+-]+)", candidate, re.I)
                if stock_match:
                    r.stock = stock_match.group(1)
                else:
                    # pick a likely stock value only if numeric and smallish
                    nums = re.findall(r"\b\d+\b", candidate)
                    if nums:
                        r.stock = nums[0]
            if not r.provider:
                # heuristic: first cell often provider/supplier
                r.provider = cells[0]
            if not r.availability:
                for marker in ("in stock", "available", "out of stock", "backorder", "available now", "limited"):
                    if marker in candidate.lower():
                        r.availability = marker
                        break
            out.append(r)
    return out


async def extract_page_metadata(page: Page, query: str) -> tuple[str, str, str]:
    text = await body_text(page)
    title = ""
    manufacturer = ""
    reference = ""
    try:
        title = normalize_text(await page.locator("h1").first.inner_text(timeout=2000))
    except Exception:
        pass
    if not title:
        # try any prominent heading mentioning the query
        for selector in ["h2", "h3", ".title", ".product-name", "header h1"]:
            try:
                loc = page.locator(selector)
                if await loc.count():
                    title = normalize_text(await loc.first.inner_text(timeout=1500))
                    if title:
                        break
            except Exception:
                continue
    # Manufacturer/reference labels in either English or German
    patterns = {
        "manufacturer": [r"manufacturer\s*[:\-]\s*([^|•\n]+)", r"hersteller\s*[:\-]\s*([^|•\n]+)"],
        "reference": [r"reference\s*[:\-]\s*([^|•\n]+)", r"artikelnummer\s*[:\-]\s*([^|•\n]+)", r"product number\s*[:\-]\s*([^|•\n]+)", r"ref\.?\s*[:\-]\s*([^|•\n]+)"],
    }
    for pat in patterns["manufacturer"]:
        m = re.search(pat, text, re.I)
        if m:
            manufacturer = normalize_text(m.group(1))
            break
    for pat in patterns["reference"]:
        m = re.search(pat, text, re.I)
        if m:
            reference = normalize_text(m.group(1))
            break
    if not title and query:
        title = query
    return title, manufacturer, reference


async def extract_visible_rows(page: Page, query: str, sort_mode: str) -> list[Row]:
    # Try product-page suppliers table first.
    rows = await extract_rows_from_tables(page)

    if not rows:
        # fallback: inspect all rows-like elements / list items for product cards
        body = await body_text(page)
        lines = [normalize_text(x) for x in re.split(r"\n|\r|\t", body) if normalize_text(x)]
        for line in lines[:400]:
            if re.search(r"[€$£]", line) or re.search(r"\b(stock|availability|available|out of stock|in stock|supplier|provider)\b", line, re.I):
                rows.append(Row(provider=line[:80], price=format_price(parse_price(line)) or "", stock=parse_stock(line), availability=line))

    # Attach page-level product metadata to each row if missing.
    title, manufacturer, reference = await extract_page_metadata(page, query)
    for r in rows:
        if not r.product:
            r.product = title
        if not r.manufacturer:
            r.manufacturer = manufacturer
        if not r.reference:
            r.reference = reference

    # Filter stock > 0 when possible.
    filtered: list[Row] = []
    for r in rows:
        stock_num = None
        if r.stock:
            m = re.search(r"\d+", r.stock)
            if m:
                try:
                    stock_num = int(m.group(0))
                except Exception:
                    stock_num = None
        if stock_num is None:
            # keep rows only if they clearly suggest availability when stock can't be parsed
            if r.availability and any(k in r.availability.lower() for k in ("in stock", "available", "available now", "limited")):
                filtered.append(r)
            continue
        if stock_num > 0:
            filtered.append(r)
    if filtered:
        rows = filtered

    # Sort by price if available.
    def price_key(r: Row):
        return parse_price(r.price or "") or -1.0

    reverse = sort_mode == "price_desc"
    rows.sort(key=price_key, reverse=reverse)
    return rows


def render_rows(rows: list[Row], limit: int = 8) -> str:
    if not rows:
        return "No structured rows found."
    header = ["product", "manufacturer", "reference", "provider", "stock", "price", "availability"]
    data = []
    for r in rows[:limit]:
        data.append([r.product, r.manufacturer, r.reference, r.provider, r.stock, r.price, r.availability])
    # build compact aligned text table for Telegram/code block.
    widths = [len(h) for h in header]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell or ""))
    def fmt_row(row):
        return " | ".join((cell or "").ljust(widths[i]) for i, cell in enumerate(row))
    lines = [fmt_row(header), "-+-".join("-" * w for w in widths)]
    for row in data:
        lines.append(fmt_row(row))
    return "\n".join(lines)


async def save_screenshot(page: Page, name: str = "itscope-last.png") -> Path:
    ensure_dirs()
    path = ARTIFACT_DIR / name
    await page.screenshot(path=str(path), full_page=True)
    return path


async def cmd_status(interactive: bool = False) -> int:
    ensure_dirs()
    state = load_state()
    p, context, page = await open_page(headless=not interactive)
    try:
        logged_in = await ensure_logged_in(page, interactive_login=interactive)
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        path = await save_screenshot(page, "status.png")
        state.logged_in = logged_in
        state.last_url = page.url
        state.last_title = title
        state.last_action = "status"
        state.screenshot_path = str(path)
        state.note = "logged-in" if logged_in else "login-required"
        save_state(state)
        print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
        return 0
    finally:
        await close_page(p, context)


async def cmd_login() -> int:
    ensure_dirs()
    try:
        p, context, page = await open_page(headless=False)
    except Exception as exc:
        print("Could not open a visible browser for manual login in this environment.")
        print("Reason:", exc)
        print("If you have a desktop/X server available, rerun the command there or use xvfb-run.")
        state = load_state()
        state.logged_in = False
        state.last_action = "login"
        state.note = "headed-browser-unavailable"
        save_state(state)
        return 3
    try:
        await page_ready(page, DEFAULT_HOME_URL)
        if not await is_login_page(page):
            state = load_state()
            state.logged_in = True
            state.last_url = page.url
            state.last_title = await page.title()
            state.last_action = "login"
            state.note = "already-logged-in"
            save_state(state)
            print("ITscope is already logged in.")
            print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
            return 0

        print("A browser window should be open for ITscope login.")
        print("Complete the login manually, then press Enter here to verify the session.")
        try:
            input()
        except EOFError:
            pass
        await page.goto(DEFAULT_HOME_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        logged_in = not await is_login_page(page)
        path = await save_screenshot(page, "login.png")
        state = load_state()
        state.logged_in = logged_in
        state.last_url = page.url
        state.last_title = await page.title()
        state.last_action = "login"
        state.screenshot_path = str(path)
        state.note = "login-complete" if logged_in else "still-login-required"
        save_state(state)
        print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
        return 0 if logged_in else 2
    finally:
        await close_page(p, context)


async def cmd_screenshot() -> int:
    ensure_dirs()
    p, context, page = await open_page(headless=True)
    try:
        await page_ready(page, DEFAULT_HOME_URL)
        path = await save_screenshot(page, "manual.png")
        state = load_state()
        state.last_url = page.url
        state.last_title = await page.title()
        state.last_action = "screenshot"
        state.screenshot_path = str(path)
        state.logged_in = not await is_login_page(page)
        state.note = "saved"
        save_state(state)
        print(str(path))
        return 0
    finally:
        await close_page(p, context)


async def cmd_search(query: str, sort_mode: str, interactive_login: bool = False, limit: int = 8) -> int:
    ensure_dirs()
    p, context, page = await open_page(headless=not interactive_login)
    try:
        logged_in = await ensure_logged_in(page, interactive_login=interactive_login)
        if not logged_in:
            state = load_state()
            state.logged_in = False
            state.last_url = page.url
            state.last_title = await page.title()
            state.last_action = "search"
            state.last_query = query
            state.note = "login-required"
            save_state(state)
            print("LOGIN_REQUIRED")
            print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
            return 2

        # Search route first.
        search_ok = await search_direct(page, query)
        if not search_ok:
            state = load_state()
            state.logged_in = False
            state.last_url = page.url
            state.last_title = await page.title()
            state.last_action = "search"
            state.last_query = query
            state.note = "login-required"
            save_state(state)
            print("LOGIN_REQUIRED")
            print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
            return 2

        # Best-effort select first matching product result.
        clicked = await maybe_click_first_product(page, query)
        if not clicked:
            # Try using the search input to trigger dropdown suggestions on the home page.
            await page.goto(DEFAULT_HOME_URL, wait_until="domcontentloaded")
            inp = await try_find_search_input(page)
            if inp is not None:
                try:
                    await inp.click(timeout=3000)
                    await inp.fill(query)
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("networkidle")
                    await maybe_click_first_product(page, query)
                except Exception:
                    pass

        # Try UI sort/filter first, but always apply code-side filtering/sorting after extraction.
        await click_stock_filter_if_present(page)
        await click_sort_if_present(page, sort_mode)

        # Give dynamic tables a moment to settle.
        await page.wait_for_timeout(800)
        rows = await extract_visible_rows(page, query, sort_mode)
        path = await save_screenshot(page, f"search-{re.sub(r'[^A-Za-z0-9]+', '-', query).strip('-') or 'query'}.png")
        state = load_state()
        state.logged_in = True
        state.last_url = page.url
        state.last_title = await page.title()
        state.last_action = "search"
        state.last_query = query
        state.screenshot_path = str(path)
        state.note = f"rows={len(rows)}"
        save_state(state)
        print(render_rows(rows, limit=limit))
        print()
        print(f"Screenshot: {path}")
        print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
        return 0
    finally:
        await close_page(p, context)


async def main_async(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="ITscope Control via Playwright (persistent session, no API)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="Search ITscope and extract visible supplier rows")
    p_search.add_argument("query", help="Query text, e.g. 5090 or RTX 5090")
    p_search.add_argument("--sort", default="price_desc", choices=["price_desc", "price_asc"], help="Price sort direction")
    p_search.add_argument("--limit", type=int, default=8, help="Max rows to print")
    p_search.add_argument("--interactive-login", action="store_true", help="Open a visible browser and wait for manual login if needed")

    p_login = sub.add_parser("login", help="Open visible browser and wait for manual login")

    p_status = sub.add_parser("status", help="Check whether the persistent session is logged in")
    p_status.add_argument("--interactive-login", action="store_true", help="Open visible browser if login is needed")

    p_ss = sub.add_parser("screenshot", help="Take a screenshot of the current ITscope page")

    args = parser.parse_args(argv)

    if args.cmd == "search":
        return await cmd_search(args.query, args.sort, interactive_login=args.interactive_login, limit=args.limit)
    if args.cmd == "login":
        return await cmd_login()
    if args.cmd == "status":
        return await cmd_status(interactive=args.interactive_login)
    if args.cmd == "screenshot":
        return await cmd_screenshot()
    return 1


def main() -> int:
    try:
        return asyncio.run(main_async(sys.argv[1:]))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
