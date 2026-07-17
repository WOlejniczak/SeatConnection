import os
import io
import re
import json
import zipfile
from pathlib import Path
import requests
from fastapi import FastAPI, HTTPException
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
PROPERTY = os.environ["PROPERTY"]
CUPRA_USER = os.environ["CUPRA_USER"]
CUPRA_PASS = os.environ["CUPRA_PASS"]
NOTION_VERSION = os.environ["NOTION_VERSION"]
VIN = os.environ["VIN"]

HEADLESS = True
PORTAL_URL = "https://eu-data-act.drivesomethinggreater.com/de/en/user.html"
DETAILS_URL = f"https://eu-data-act.drivesomethinggreater.com/content/euda/de/en/user/details?vin={VIN}"


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def extract_json_bytes(raw: bytes, name: str) -> dict:
    if name.lower().endswith(".zip") or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not names:
                raise ValueError("No .json in archive")
            raw = zf.read(names[0])
    return json.loads(raw)


def parse_mileage(payload: dict) -> int:
    for entry in payload.get("Data", []):
        if entry.get("dataFieldName") == "mileage":
            return int(entry["value"])
    raise ValueError("mileage field not found")


async def _full_login(page):
    """Pełny OIDC flow — wykonywany zawsze (świeża sesja przy każdym uruchomieniu)."""
    await page.goto(PORTAL_URL, wait_until="networkidle")
    await page.get_by_role("link", name="Log In").first.click()

    brand = page.locator("#form-login-brand-select")
    await brand.wait_for(state="attached", timeout=15000)
    await brand.select_option(value="SEAT")
    await brand.dispatch_event("change")
    await page.locator("#form-login-cta-login").click()

    cont = page.locator("#redirect-screen-cta-continue")
    await cont.wait_for(state="visible", timeout=15000)
    await cont.click()

    email = page.locator("#input_email")
    await email.wait_for(state="visible", timeout=20000)
    await email.fill(CUPRA_USER)
    await email.press("Enter")

    pwd = page.locator("#password")
    await pwd.wait_for(state="visible", timeout=20000)
    await pwd.fill(CUPRA_PASS)
    async with page.expect_navigation(wait_until="networkidle", timeout=45000):
        await pwd.press("Enter")

    # Consent — obsłużony defensywnie, na wypadek gdyby wyskoczył
    try:
        consent = page.locator("button[type=submit], button#submit").filter(
            has_text=re.compile(r"allow|zaakceptuj|weiter|accept", re.I)
        )
        if await consent.count() and await consent.first.is_visible():
            async with page.expect_navigation(wait_until="networkidle", timeout=30000):
                await consent.first.click()
    except Exception:
        pass


async def get_mileage() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # Zawsze świeże, pełne logowanie
            await _full_login(page)

            # Po zalogowaniu przejdź na stronę szczegółów pojazdu
            await page.goto(DETAILS_URL, wait_until="networkidle")

            download_btn = page.get_by_test_id("request-file-partial-file-download-button")
            await download_btn.wait_for(state="visible", timeout=20000)

            async with page.expect_download(timeout=30000) as dl_info:
                await download_btn.click()
            download = await dl_info.value

            path = await download.path()
            content = Path(path).read_bytes()

            return parse_mileage(extract_json_bytes(content, download.suggested_filename))

        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"Timed out in scraping flow (url={page.url}): {e}") from e
        finally:
            await context.close()
            await browser.close()


def get_all_page_ids() -> list[str]:
    page_ids = []
    has_more = True
    start_cursor = None

    while has_more:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        r = requests.post(
            f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
            headers=notion_headers(),
            json=payload,
        )
        if not r.ok:
            raise RuntimeError(f"Failed to query database: {r.text}")

        data = r.json()
        page_ids.extend([page["id"] for page in data.get("results", [])])
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return page_ids


def update_notion(value: int):
    page_ids = get_all_page_ids()
    if not page_ids:
        raise ValueError("Database is empty - no rows to update")

    failed_updates = []
    for page_id in page_ids:
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            json={"properties": {PROPERTY: {"number": value}}},
        )
        if not r.ok:
            failed_updates.append({"page_id": page_id, "error": r.text})

    if failed_updates:
        raise RuntimeError(f"Failed to update {len(failed_updates)} rows: {json.dumps(failed_updates)}")


@app.post("/run")
async def run():
    try:
        mileage = await get_mileage()
        update_notion(mileage)
        return {"mileage": mileage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import asyncio
    print("start")
    mileage = asyncio.run(get_mileage())
    print("mileage:", mileage)
    update_notion(mileage)
    print("Notion zaktualizowany")