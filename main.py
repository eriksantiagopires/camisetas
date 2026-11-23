import json
import os
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote, unquote
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ORDERS_FILE = "orders.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Cache-Control": "no-cache",
}

TEAM_SLUGS = {
    "real madrid": "real-madrid",
    "fc barcelona": "fc-barcelona",
    "barcelona": "fc-barcelona",
    "atletico": "atletico-madrid",
    "atletico de madrid": "atletico-madrid",
    "celta": "celta-vigo",
    "celta de vigo": "celta-vigo",
    "rc celta": "celta-vigo",
    "rc celta de vigo": "celta-vigo",
    "benfica": "sl-benfica",
    "sl benfica": "sl-benfica",
    "sport lisboa e benfica": "sl-benfica",
    "espana": "spain",
    "spain": "spain",
    "argentina": "argentina",
    "portugal": "portugal",
    "man city": "manchester-city",
    "manchester city": "manchester-city",
    "psg": "paris-saint-germain",
    "paris saint germain": "paris-saint-germain",
}


class OrderItem(BaseModel):
    team: str
    season: str
    kit: str
    size: str
    patch: str
    dorsalText: str
    price: float
    img: str


class OrderCreate(BaseModel):
    customer: str
    phone: str
    total: float
    items: list[OrderItem]


class StatusUpdate(BaseModel):
    status: str


def load_orders() -> list[dict]:
    if not os.path.exists(ORDERS_FILE):
        return []
    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_orders(orders: list[dict]) -> None:
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)


def slugify(text: str) -> str:
    clean = unicodedata.normalize("NFD", text.strip().lower())
    clean = "".join(c for c in clean if unicodedata.category(c) != "Mn")
    if clean in TEAM_SLUGS:
        return TEAM_SLUGS[clean]
    return re.sub(r"[^a-z0-9]+", "-", clean).strip("-")


def format_season(season: str) -> str:
    s = season.strip().replace("/", "-")
    m = re.match(r"^(\d{4})-20(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return s


@app.get("/api/kit")
async def search_kits(
    team: str = Query(...),
    season: str = Query("2026/27"),
    kit_type: str = Query("1ª Equipación"),
):
    team_slug = slugify(team)
    season_slug = format_season(season)
    kits = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
        # 1. Intentar Football Kit Archive buscando cualquier imagen de equipación en el HTML
        fka_urls = [
            f"https://www.footballkitarchive.com/{team_slug}-{season_slug}-kits/",
            f"https://www.footballkitarchive.com/{team_slug}-2025-26-kits/",
            f"https://www.footballkitarchive.com/{team_slug}-2024-25-kits/",
        ]
        for url in fka_urls:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    for img in soup.find_all("img"):
                        src = img.get("data-src") or img.get("src") or ""
                        if "footballkitarchive" not in src.lower():
                            continue
                        if any(bad in src.lower() for bad in ("logo", "avatar", "flag", "icon", "badge")):
                            continue
                        if src.startswith("//"):
                            src = "https:" + src
                        label = (img.get("alt") or f"{team} ({season})").strip()
                        proxied = f"/api/img?url={quote(src, safe='')}"
                        if not any(k["raw"] == src for k in kits):
                            kits.append({"url": proxied, "raw": src, "label": label})
                    if kits:
                        break
            except Exception:
                pass

        # 2. TheSportsDB: traer todas las equipaciones oficiales disponibles del equipo
        queries = [team_slug.replace("-", " "), team]
        seen_teams = set()
        for q in queries:
            try:
                rt = await client.get(
                    f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t={quote(q)}"
                )
                if rt.status_code == 200:
                    for t in (rt.json().get("teams") or [])[:2]:
                        tid = t.get("idTeam")
                        if not tid or tid in seen_teams:
                            continue
                        seen_teams.add(tid)
                        re_q = await client.get(
                            f"https://www.thesportsdb.com/api/v1/json/3/lookupequipment.php?id={tid}"
                        )
                        if re_q.status_code == 200:
                            for eq in re_q.json().get("equipment") or []:
                                eq_url = eq.get("strEquipment")
                                if eq_url:
                                    eq_season = eq.get("strSeason") or "Oficial"
                                    kits.append({
                                        "url": f"/api/img?url={quote(eq_url, safe='')}",
                                        "raw": eq_url,
                                        "label": f"{t.get('strTeam', team)} · {eq_season}",
                                    })
            except Exception:
                pass

    priority_keywords = {
        "1": ["home", "1st", "local"],
        "2": ["away", "2nd", "visitante"],
        "3": ["third", "3rd", "tercera"],
        "4": ["fourth", "4th"],
        "p": ["gk", "goalkeeper", "portero"],
    }
    keys = priority_keywords.get(kit_type.strip().lower()[:1], ["home"])

    def sort_key(item):
        lbl = item["label"].lower()
        is_kit_type = 0 if any(k in lbl for k in keys) else 1
        is_season = 0 if season.replace("/", "-") in lbl.replace("/", "-") else 1
        return (is_kit_type, is_season)

    kits.sort(key=sort_key)
    return {"kits": [{"url": k["url"], "label": k["label"]} for k in kits]}


@app.get("/api/img")
async def proxy_image(url: str = Query(...)):
    decoded_url = unquote(url)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
        r = await client.get(decoded_url)
        if r.status_code == 200:
            return Response(
                content=r.content,
                media_type=r.headers.get("content-type", "image/png"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    return Response(status_code=404)


@app.get("/api/orders")
async def get_orders():
    return {"orders": load_orders()}


@app.post("/api/orders")
async def create_order(order: OrderCreate):
    orders = load_orders()
    new_order = {
        "id": f"PED-{100 + len(orders) + 1}",
        "created_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "customer": order.customer,
        "phone": order.phone,
        "status": "En espera",
        "total": order.total,
        "items": [item.model_dump() for item in order.items],
    }
    orders.insert(0, new_order)
    save_orders(orders)
    return {"order": new_order}


@app.patch("/api/orders/{order_id}")
async def patch_order(order_id: str, payload: StatusUpdate):
    if payload.status not in ("En espera", "Vendida"):
        raise HTTPException(status_code=400, detail="Estado no válido")
    orders = load_orders()
    for o in orders:
        if o["id"] == order_id:
            o["status"] = payload.status
            save_orders(orders)
            return {"order": o}
    raise HTTPException(status_code=404, detail="Pedido no encontrado")


@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")
