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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.footballkitarchive.com/",
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
    "deportivo": "deportivo-la-coruna",
    "deportivo de la coruna": "deportivo-la-coruna",
    "benfica": "sl-benfica",
    "sl benfica": "sl-benfica",
    "sport lisboa e benfica": "sl-benfica",
    "porto": "fc-porto",
    "fc porto": "fc-porto",
    "sporting": "sporting-cp",
    "sporting cp": "sporting-cp",
    "espana": "spain",
    "spain": "spain",
    "argentina": "argentina",
    "portugal": "portugal",
    "francia": "france",
    "france": "france",
    "brasil": "brazil",
    "brazil": "brazil",
    "alemania": "germany",
    "italia": "italy",
    "inglaterra": "england",
    "man city": "manchester-city",
    "manchester city": "manchester-city",
    "man united": "manchester-united",
    "manchester united": "manchester-united",
    "arsenal": "arsenal-fc",
    "liverpool": "liverpool-fc",
    "chelsea": "chelsea-fc",
    "psg": "paris-saint-germain",
    "paris saint germain": "paris-saint-germain",
    "bayern": "fc-bayern-munchen",
    "bayern munich": "fc-bayern-munchen",
    "inter": "inter-milan",
    "milan": "ac-milan",
    "juventus": "juventus-fc",
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
    target_url = f"https://www.footballkitarchive.com/{team_slug}-{season_slug}-kits/"

    kits = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
        resp = await client.get(target_url)

        if resp.status_code != 200:
            search_resp = await client.get(
                f"https://www.footballkitarchive.com/search/?q={quote(team)}"
            )
            if search_resp.status_code == 200:
                soup_s = BeautifulSoup(search_resp.text, "html.parser")
                for a in soup_s.select("a[href*='-kits/']"):
                    href = a.get("href", "")
                    if season_slug in href or team_slug.split("-")[0] in href:
                        full_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.footballkitarchive.com{href}"
                        )
                        resp = await client.get(full_url)
                        if resp.status_code == 200:
                            break

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select(".kit-container, .collection-item, a[href*='-kit/']"):
                img = card.find("img")
                if not img:
                    continue
                src = img.get("data-src") or img.get("src") or ""
                if not src or "logo" in src.lower() or "placeholder" in src.lower():
                    continue
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = "https://www.footballkitarchive.com" + src

                label = (img.get("alt") or card.get_text(" ", strip=True) or "Football Kit Archive").strip()
                proxied = f"/api/img?url={quote(src, safe='')}"
                if not any(k["raw"] == src for k in kits):
                    kits.append({"url": proxied, "raw": src, "label": label})

        # Respaldo automático con TheSportsDB si FKA aún no indexó esa temporada
        if not kits:
            tsdb_query = team_slug.replace("-", " ")
            r_team = await client.get(
                f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t={quote(tsdb_query)}"
            )
            if r_team.status_code == 200:
                teams_data = r_team.json().get("teams") or []
                if teams_data:
                    team_id = teams_data[0]["idTeam"]
                    r_eq = await client.get(
                        f"https://www.thesportsdb.com/api/v1/json/3/lookupequipment.php?id={team_id}"
                    )
                    if r_eq.status_code == 200:
                        for eq in r_eq.json().get("equipment") or []:
                            eq_url = eq.get("strEquipment")
                            if eq_url:
                                kits.append({
                                    "url": f"/api/img?url={quote(eq_url, safe='')}",
                                    "raw": eq_url,
                                    "label": f"{teams_data[0]['strTeam']} ({eq.get('strSeason', 'Oficial')})",
                                })

    priority_keywords = {
        "1": ["home", "1st", "local"],
        "2": ["away", "2nd", "visitante"],
        "3": ["third", "3rd", "tercera"],
        "4": ["fourth", "4th"],
        "p": ["gk", "goalkeeper", "portero"],
    }
    first_char = kit_type.strip().lower()[:1]
    keys = priority_keywords.get(first_char, ["home"])

    kits.sort(
        key=lambda item: 0
        if any(k in item["label"].lower() for k in keys)
        else 1
    )

    return {"kits": [{"url": k["url"], "label": k["label"]} for k in kits]}


@app.get("/api/img")
async def proxy_image(url: str = Query(...)):
    decoded_url = unquote(url)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
        r = await client.get(decoded_url)
        if r.status_code == 200:
            return Response(
                content=r.content,
                media_type=r.headers.get("content-type", "image/webp"),
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