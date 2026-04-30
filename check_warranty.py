"""Check Zebra device warranty status for one or more serial numbers.

Reads serial numbers from a CSV (one per line in the first column, header
optional), drives https://support.zebra.com/warrantycheck via Playwright, and
writes the captured warranty API JSON responses to an output file.

The page is built with Salesforce LWC and protected by an invisible reCAPTCHA.
The warranty data comes from a single Apex call:
  POST /webruntime/api/apex/execute   method="serialCalloutEVM"
The response body's `returnValue.serialNumberDetails.asset[]` contains all the
warranty info shown in the UI, plus more.

Usage:
    uv run check_warranty.py serials.csv -o results.json
    uv run check_warranty.py serials.csv -o results.json --headless

Note: --headless tends to fail reCAPTCHA's bot detection (the apex call comes
back 400). The default (headful) is much more reliable.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

WARRANTY_URL = "https://support.zebra.com/warrantycheck"
APEX_PATH = "/webruntime/api/apex/execute"
WARRANTY_METHOD = "serialCalloutEVM"

REALISTIC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def read_serials(csv_path: Path) -> list[str]:
    """Return non-empty serial numbers from the first column of *csv_path*.

    A first row that looks like a header (contains the word "serial") is skipped.
    """
    serials: list[str] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if not row:
                continue
            value = row[0].strip()
            if not value:
                continue
            if i == 0 and "serial" in value.lower():
                continue
            serials.append(value)
    return serials


async def dismiss_cookie_banner(page: Page) -> None:
    """Click 'Accept Cookies' on the OneTrust banner and wait for the overlay to clear.

    The OneTrust SDK injects a full-page dark filter that intercepts clicks until
    the user accepts/rejects, so we must not proceed until that overlay is gone.
    """
    try:
        btn = page.locator("#onetrust-accept-btn-handler").first
        try:
            await btn.wait_for(state="visible", timeout=10000)
        except PWTimeoutError:
            return  # No banner this run (e.g., already accepted via cookie).
        await btn.click()
        # Wait for the dark filter / consent SDK to stop intercepting clicks.
        try:
            await page.locator(".onetrust-pc-dark-filter").wait_for(
                state="hidden", timeout=8000
            )
        except PWTimeoutError:
            pass
        # Belt-and-braces: also confirm the banner button itself is gone.
        try:
            await btn.wait_for(state="hidden", timeout=3000)
        except PWTimeoutError:
            pass
    except Exception:
        pass


async def wait_for_form(page: Page) -> None:
    serial_input = page.locator("input[placeholder='Serial Number']").first
    await serial_input.wait_for(state="visible", timeout=20000)


async def check_one_serial(page: Page, serial: str, timeout_ms: int) -> dict[str, Any]:
    """Submit *serial* and return the parsed warranty JSON, or an error dict."""
    captured: dict[str, Any] = {}
    done = asyncio.Event()

    async def on_response(response: Response) -> None:
        if APEX_PATH not in response.url:
            return
        post = response.request.post_data
        if not post or WARRANTY_METHOD not in post:
            return
        try:
            data = await response.json()
        except Exception as e:
            captured["error"] = f"failed to parse JSON: {e}"
            done.set()
            return
        captured["status"] = response.status
        captured["data"] = data
        done.set()

    listener = lambda r: asyncio.create_task(on_response(r))
    page.on("response", listener)
    try:
        serial_input = page.locator("input[placeholder='Serial Number']").first
        await serial_input.wait_for(state="visible", timeout=10000)
        await serial_input.click()
        await serial_input.fill("")
        await serial_input.fill(serial)

        search_btn = page.locator("button:has-text('Search')").first
        await search_btn.wait_for(state="visible", timeout=5000)
        await search_btn.click()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            return {
                "serial_number": serial,
                "error": f"timeout after {timeout_ms} ms waiting for {WARRANTY_METHOD} response",
            }

        if "error" in captured:
            return {"serial_number": serial, "error": captured["error"]}

        status = captured["status"]
        data = captured["data"]
        if status != 200:
            return {
                "serial_number": serial,
                "error": f"warranty API returned HTTP {status}",
                "response": data,
            }
        return {
            "serial_number": serial,
            "response": data.get("returnValue", data),
        }
    except Exception as e:
        return {"serial_number": serial, "error": f"{type(e).__name__}: {e}"}
    finally:
        page.remove_listener("response", listener)


async def reset_form(page: Page) -> None:
    """Reload the page to clear any prior result; cookie cookie banner is already accepted."""
    await page.goto(WARRANTY_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    await wait_for_form(page)


async def run(serials: list[str], output: Path, headless: bool, timeout_ms: int) -> None:
    results: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=REALISTIC_UA,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print(f"Opening {WARRANTY_URL} ...", file=sys.stderr)
        await page.goto(WARRANTY_URL, wait_until="domcontentloaded")
        await dismiss_cookie_banner(page)
        await wait_for_form(page)

        for i, serial in enumerate(serials, 1):
            print(f"[{i}/{len(serials)}] checking {serial} ...", file=sys.stderr)
            if i > 1:
                await reset_form(page)
            result = await check_one_serial(page, serial, timeout_ms)
            if "error" in result:
                print(f"  -> ERROR: {result['error']}", file=sys.stderr)
            else:
                # Print a brief summary line.
                rv = result.get("response", {})
                msg = rv.get("message") or ""
                num = rv.get("numOfRecords") or "?"
                print(f"  -> ok ({num} record(s)){' - ' + msg if msg else ''}", file=sys.stderr)
            results.append(result)

        await context.close()
        await browser.close()

    output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(results)} result(s) to {output}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("input_csv", type=Path, help="CSV file with serial numbers in column 1")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("warranty_results.json"),
        help="Output JSON path (default: warranty_results.json)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser headlessly (often fails reCAPTCHA; default is headful)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30000,
        help="Per-serial wait for the API response in ms (default: 30000)",
    )
    args = parser.parse_args()

    if not args.input_csv.exists():
        parser.error(f"input file not found: {args.input_csv}")

    serials = read_serials(args.input_csv)
    if not serials:
        parser.error("no serial numbers found in input CSV")

    asyncio.run(run(serials, args.output, args.headless, args.timeout))


if __name__ == "__main__":
    main()
