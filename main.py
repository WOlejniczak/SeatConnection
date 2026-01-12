import os
import json
import aiohttp
import asyncio
import requests
from fastapi import FastAPI, HTTPException
from pycupra import Connection

app = FastAPI()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
PROPERTY = os.environ["PROPERTY"]
CUPRA_USER = os.environ["CUPRA_USER"]
CUPRA_PASS = os.environ["CUPRA_PASS"]
VIN = os.environ["VIN"]
BASE_URL = "https://ola.prod.code.seat.cloud.vwgroup.com"

async def get_mileage() -> int:
    async with aiohttp.ClientSession() as session:
        client = Connection(
            session=session,
            username=CUPRA_USER,
            password=CUPRA_PASS,
            country="PL",
            language="pl-PL",
        )
        await client.doLogin()
        data = await client.getMileage(VIN, BASE_URL)
        mileage = data.get("mileage", {}).get("mileageKm")
        if mileage is None:
            raise ValueError("Mileage not found")
        return mileage

def get_all_page_ids() -> list[str]:
    """Pobiera wszystkie ID wierszy z bazy danych"""
    page_ids = []
    has_more = True
    start_cursor = None
    
    while has_more:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor
            
        r = requests.post(
            f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            },
            json=payload
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
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            },
            json={
                "properties": {
                    PROPERTY: {"number": value}
                }
            }
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
