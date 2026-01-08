import os
import json
import aiohttp
import asyncio
import requests
from fastapi import FastAPI, HTTPException
from pycupra import Connection

app = FastAPI()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
PAGE_ID = os.environ["PAGE_ID"]
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

def update_notion(value: int):
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
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
        raise RuntimeError(r.text)

@app.post("/run")
async def run():
    try:
        mileage = await get_mileage()
        update_notion(mileage)
        return {"mileage": mileage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
