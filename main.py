import io
import json
import logging
import os
import re
import time
import zipfile
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp
import requests
from fastapi import FastAPI, HTTPException

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI()
_LOGGER = logging.getLogger("euda")

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
PROPERTY = os.environ["PROPERTY"]
NOTION_VERSION = os.environ["NOTION_VERSION"]
VIN = os.environ["VIN"]

EUDA_EMAIL = os.environ["CUPRA_USER"]
EUDA_PASSWORD = os.environ["CUPRA_PASS"]
EUDA_BRAND = "seat"

# --- EU Data Act portal constants (undocumented, reverse-engineered) ------
BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
OIDC_AUTHORIZE_URL = f"{IDENTITY_BASE}/oidc/v1/authorize"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = f"{BASE_URL}/login"

BRANDS = {
    "volkswagen": {"client_id": "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com", "state": "VOLKSWAGEN_PASSENGER_CARS"},
    "audi": {"client_id": "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com", "state": "AUDI"},
    "skoda": {"client_id": "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com", "state": "SKODA"},
    "seat": {"client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "state": "SEAT"},
    "cupra": {"client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "state": "CUPRA"},
}
DEFAULT_COUNTRY = "pl"
DEFAULT_LANGUAGE = "pl"

VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"
NO_CONTENT_SUFFIX = "_no_content_found.zip"

# Sentinel values the portal emits when a data point has no current reading.
# Without filtering these leak through as e.g. "mileage: 4294967295".
GENERIC_INT_SENTINELS = {65535, 2147483647, 4294967295}
MINUS_ONE_SENTINEL_KEYS = {
    "remaining_charging_time",
    "remaining_charging_time_to_complete",
    "battery_state_report.remaining_charging_time_complete",
    "battery_state_report.remaining_charging_time_bulk",
    "remaining_charging_time_target_soc",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


def _parse_form(html: str) -> _FormParser:
    p = _FormParser()
    p.feed(html)
    return p


def _extract_template_model(html: str) -> dict:
    idx = html.find("templateModel")
    if idx == -1:
        return {}
    brace = html.find("{", idx)
    if brace == -1:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except ValueError:
                    return {}
    return {}


def _extract_csrf(html: str) -> str | None:
    m = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else None


def _login_fields(html: str) -> tuple[dict[str, str], str | None]:
    form = _parse_form(html)
    fields: dict[str, str] = dict(form.fields)
    model = _extract_template_model(html)
    if model:
        for key in ("hmac", "relayState"):
            if model.get(key):
                fields[key] = model[key]
        email = (model.get("emailPasswordForm") or {}).get("email")
        if email:
            fields.setdefault("email", email)
    csrf = _extract_csrf(html)
    if csrf:
        fields.setdefault("_csrf", csrf)
    return fields, form.action


def _login_error(html: str) -> str | None:
    model = _extract_template_model(html)
    err = model.get("error") or model.get("errorCode")
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or str(err)
    return str(err) if err else None


def _diagnose_login_failure(landing_url: str, landing_html: str) -> str:
    """Map a failed-login landing page to an actionable message.

    Ported from ioBroker.vw-connect's euDataAct.js diagnoseLoginFailure.
    The most common case for a first-time API login is the VW Identity
    consent screen: correct credentials, but the account never clicked
    "Allow" for this account in a browser, so the flow halts there.
    """
    if "/signin-service/v1/consent/" in landing_url or "consent-screen" in landing_html:
        return (
            "EU Data Act consent screen is blocking the login - open "
            f"{BASE_URL}/ in a browser, log in with the same credentials, "
            "click Allow, then finish portal setup (vehicle linking + "
            "continuous 15-min data request)."
        )
    m = re.search(r"error=([^&]+)", urlparse(landing_url).query)
    err_code = m.group(1) if m else None
    text = err_code or _login_error(landing_html) or ""
    if re.search(r"password_invalid", text, re.I):
        return "Login failed: password incorrect."
    if re.search(r"throttle|rate_limit|too_many", text, re.I):
        return "Login failed: too many attempts, account throttled - wait ~30 min."
    if re.search(r"account_disabled|locked|blocked", text, re.I):
        return "Login failed: VW account is locked/disabled - reset password at identity.vwgroup.io."
    if text:
        return f"Login failed: {text}"
    return f"Login failed (no error code reported). Landing URL: {landing_url}"


class EudaClient:
    """OIDC login + data-delivery client for the VW Group EU Data Act portal."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str, brand: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._brand = brand

    async def _get(self, url: str, *, headers: dict | None = None):
        h = {"User-Agent": USER_AGENT, **(headers or {})}
        return await self._session.get(url, headers=h)

    def _authorize_url(self) -> str:
        b = BRANDS.get(self._brand, BRANDS["volkswagen"])
        params = {
            "client_id": b["client_id"],
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": f"{DEFAULT_COUNTRY}__{DEFAULT_LANGUAGE}__{b['state']}",
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    async def login(self) -> None:
        _LOGGER.info("EUDA login: starting (brand=%s)", self._brand)
        try:
            async with await self._get(f"{BASE_URL}/") as resp:
                await resp.read()
        except aiohttp.ClientError:
            pass  # priming request, failure is non-fatal

        async with await self._get(self._authorize_url()) as resp:
            signin_url = str(resp.url)
            signin_html = await resp.text()
        _LOGGER.debug("EUDA login: got sign-in page (%d bytes)", len(signin_html))

        fields, action = _login_fields(signin_html)
        if "hmac" not in fields or "_csrf" not in fields:
            raise AuthError(f"Could not parse sign-in form (fields: {sorted(fields)})")
        fields["email"] = self._email
        async with self._session.post(
            urljoin(signin_url, action or ""),
            data=fields,
            headers={"User-Agent": USER_AGENT, "Referer": signin_url},
        ) as resp:
            authenticate_url = str(resp.url)
            authenticate_html = await resp.text()
        _LOGGER.debug("EUDA login: identifier step done, got password form")

        fields2, action2 = _login_fields(authenticate_html)
        if "hmac" not in fields2 or "_csrf" not in fields2:
            raise AuthError(_login_error(authenticate_html) or "Password form not returned - check email")
        fields2["email"] = self._email
        fields2["password"] = self._password
        authenticate_action = urljoin(authenticate_url, action2) if action2 else authenticate_url.split("?", 1)[0]

        async with self._session.post(
            authenticate_action,
            data=fields2,
            headers={"User-Agent": USER_AGENT, "Referer": authenticate_url},
        ) as resp:
            landing = str(resp.url)
            landing_html = await resp.text()
            if resp.status >= 400:
                raise AuthError(_diagnose_login_failure(landing, landing_html))

        if "signin-service" in landing or "/error" in landing:
            raise AuthError(_diagnose_login_failure(landing, landing_html))
        if urlparse(landing).netloc != urlparse(BASE_URL).netloc:
            raise AuthError(f"Login did not complete (ended at {landing})")
        _LOGGER.info("EUDA login: OK")

    async def _get_json(self, url: str, *, headers: dict | None = None, _retried: bool = False):
        async with await self._get(url, headers=headers) as resp:
            status = resp.status
            text = await resp.text()
        # The portal sits behind Adobe AEM, which on an expired session can
        # return HTTP 5xx with an HTML error page instead of a clean 401/403.
        # Detect that case too, or re-login never triggers on session expiry.
        looks_like_aem_html = status >= 500 and text.lstrip().startswith("<")
        if (status in (401, 403) or looks_like_aem_html) and not _retried:
            _LOGGER.info("Session expired (HTTP %s) for %s; re-authenticating", status, url)
            await self.login()
            return await self._get_json(url, headers=headers, _retried=True)
        if status >= 400:
            raise ApiError(f"GET {url} -> HTTP {status}: {text[:300]}")
        return json.loads(text)

    async def get_metadata(self, vin: str) -> dict:
        return await self._get_json(f"{BASE_URL}{METADATA_PATH.format(vin=vin)}")

    async def list_datasets(self, vin: str, identifier: str) -> list[dict]:
        data = await self._get_json(
            f"{BASE_URL}{LIST_PATH.format(vin=vin, identifier=identifier)}",
            headers={"type": "partial"},
        )
        return data if isinstance(data, list) else data.get("files", [])

    async def download_dataset(self, vin: str, identifier: str, name: str, _retried: bool = False) -> dict:
        if name.endswith(NO_CONTENT_SUFFIX):
            raise ApiError(f"{name} contains no content")
        url = f"{BASE_URL}{DOWNLOAD_PATH.format(vin=vin, identifier=identifier)}"
        async with await self._get(url, headers={"filename": name, "type": "partial"}) as resp:
            status = resp.status
            raw = await resp.read()
        looks_like_aem_html = status >= 500 and raw[:1] == b"<"
        if (status in (401, 403) or looks_like_aem_html) and not _retried:
            _LOGGER.info("Session expired (HTTP %s) downloading %s; re-authenticating", status, name)
            await self.login()
            return await self.download_dataset(vin, identifier, name, _retried=True)
        if status >= 400:
            raise ApiError(f"Download {name} -> HTTP {status}")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not members:
                raise ApiError(f"No JSON inside {name}")
            with zf.open(members[0]) as fh:
                return json.loads(fh.read().decode("utf-8"))


def _extract_mileage(dataset: dict) -> int:
    """Extract mileage from the raw dataset's flat `Data` array.

    The portal bundles several report snapshots per 15-min dataset, so the
    same dataFieldName (e.g. "mileage.value") can appear multiple times with
    different values, and some entries are protocol sentinels (65535,
    2147483647, 4294967295) rather than real readings. Mileage is
    monotonically non-decreasing, so among usable duplicates the largest
    value is the freshest. Logic ported from ioBroker.vw-connect's
    euDataAct.js (normalizeDataset / MONOTONIC_FIELDS / sentinel filter).
    """
    best: float | None = None
    for item in dataset.get("Data", []):
        field = item.get("dataFieldName") or item.get("key")
        if field not in ("mileage.value", "mileage"):
            continue
        try:
            val = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if int(val) in GENERIC_INT_SENTINELS:
            continue
        if best is None or val > best:
            best = val
    if best is None:
        raise RuntimeError("No usable mileage reading in dataset (only sentinels or field missing)")
    return int(best)


async def get_mileage() -> int:
    async with aiohttp.ClientSession() as session:
        client = EudaClient(session, EUDA_EMAIL, EUDA_PASSWORD, EUDA_BRAND)
        await client.login()

        _LOGGER.info("EUDA: fetching metadata for VIN %s", VIN)
        metadata = await client.get_metadata(VIN)
        identifier = metadata.get("Identifier") or metadata.get("identifier")
        if not identifier:
            raise RuntimeError(f"No data-request identifier for VIN {VIN} - "
                                f"is a continuous 15-min request enabled in the portal?")
        _LOGGER.info("EUDA: got identifier %s", identifier)

        datasets = await client.list_datasets(VIN, identifier)
        datasets = [d for d in datasets if not d.get("name", "").endswith(NO_CONTENT_SUFFIX)]
        if not datasets:
            raise RuntimeError("No datasets available yet for this vehicle")
        latest = max(datasets, key=lambda d: d.get("createdOn", ""))
        _LOGGER.info("EUDA: %d dataset(s) available, downloading latest: %s", len(datasets), latest["name"])

        payload = await client.download_dataset(VIN, identifier, latest["name"])
        mileage = _extract_mileage(payload)
        _LOGGER.info("EUDA: mileage=%d km", mileage)
        return mileage


# --- Notion (unchanged) ----------------------------------------------------

def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


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
    _LOGGER.info("EUDA: updating %d Notion row(s) to mileage=%d", len(page_ids), value)

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


_LAST_RUN: dict = {"status": "never_run"}


@app.get("/status")
async def status():
    return _LAST_RUN


@app.post("/run")
async def run():
    started = time.time()
    _LOGGER.info("=== /run triggered ===")
    try:
        mileage = await get_mileage()
        update_notion(mileage)
        _LAST_RUN.update(
            status="ok",
            mileage=mileage,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            duration_s=round(time.time() - started, 1),
        )
        _LOGGER.info("=== /run OK: mileage=%d, took %.1fs ===", mileage, time.time() - started)
        return {"mileage": mileage}
    except Exception as e:
        _LAST_RUN.update(
            status="error",
            error=str(e),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            duration_s=round(time.time() - started, 1),
        )
        _LOGGER.exception("=== /run FAILED after %.1fs ===", time.time() - started)
        raise HTTPException(status_code=500, detail=str(e))