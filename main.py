import json
import os
import unicodedata
from datetime import datetime
from pathlib import Path
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
KITS_DIR = STATIC_DIR / "kits"
KITS_DIR.mkdir(parents=True, exist_ok=True)

CAMISETAS_JSON = BASE_DIR / "camisetas.json"
ORDERS_FILE = BASE_DIR / "orders.json"

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()
CACHE: dict[str, list[dict]] = {}


class AdminLogin(BaseModel):
    password: str


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


def normalizar(texto: str) -> str:
    clean = unicodedata.normalize("NFD", (texto or "").strip().lower())
    return "".join(c for c in clean if unicodedata.category(c) != "Mn")


def mapear_tipo(kit_type: str) -> str:
    t = normalizar(kit_type)
    if any(k in t for k in ("2", "away", "visitante", "segunda", "fuera")):
        return "away"
    if any(k in t for k in ("3", "third", "tercera", "alternativa")):
        return "third"
    if any(k in t for k in ("gk", "portero", "arquero", "goalkeeper")):
        return "gk"
    return "home"


def temporadas_para_anio(anio: str) -> list[str]:
    anio = (anio or "").strip()
    if len(anio) == 4 and anio.isdigit():
        y = int(anio)
        y1 = str(y + 1)[-2:]
        y0 = str(y - 1)[-2:]
        return [
            f"{y}-{y1}",
            f"{y}/{y1}",
            f"{y}-{y + 1}",
            f"{y}/{y + 1}",
            f"{y - 1}-{str(y)[-2:]}",
            f"{y - 1}/{str(y)[-2:]}",
            f"{str(y)[-2:]}/{y1}",
            f"{str(y)[-2:]}-{y1}",
            f"{y0}/{str(y)[-2:]}",
            str(y),
        ]
    return [anio] if anio else []


def cargar_catalogo_local() -> list[dict]:
    if not CAMISETAS_JSON.exists():
        return []
    try:
        data = json.loads(CAMISETAS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def buscar_en_catalogo_local(equipo: str, anio: str, tipo_code: str) -> list[dict]:
    eq_norm = normalizar(equipo)
    anio_norm = normalizar(anio)
    temps = [normalizar(t) for t in temporadas_para_anio(anio)]
    coincidencias = []

    for item in cargar_catalogo_local():
        nombres = [normalizar(item.get("equipo", ""))] + [
            normalizar(a) for a in (item.get("alias") or [])
        ]
        if not any(eq_norm in n or n in eq_norm for n in nombres if n):
            continue

        item_tipo = normalizar(item.get("tipo", "home"))
        item_anio = normalizar(str(item.get("anio", "")))
        item_titulo = normalizar(item.get("titulo", ""))

        score = 0
        if item_tipo == tipo_code:
            score += 10
        if anio_norm and (item_anio == anio_norm or any(t in item_titulo for t in temps)):
            score += 10

        coincidencias.append((score, {
            "url": item.get("imagen", ""),
            "label": item.get("titulo") or f"{item.get('equipo')} ({item.get('anio')})",
        }))

    coincidencias.sort(key=lambda x: x[0], reverse=True)
    return [c[1] for c in coincidencias if c[1]["url"]]


def buscar_en_google_imagenes(consulta: str) -> list[dict]:
    if not SERPER_API_KEY:
        return []
    try:
        r = requests.post(
            "https://google.serper.dev/images",
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": consulta, "num": 10},
            timeout=12,
        )
        r.raise_for_status()
        raw = r.json().get("images") or []
        limpios = []
        for idx, item in enumerate(raw):
            url = item.get("imageUrl") or ""
            title = item.get("title") or ""
            w = item.get("imageWidth") or 0
            h = item.get("imageHeight") or 0
            if url.startswith("https://"):
                limpios.append({
                    "id": idx,
                    "title": title,
                    "imageUrl": url,
                    "width": w,
                    "height": h,
                })
        return limpios
    except requests.RequestException:
        return []


def elegir_con_ia(equipo: str, anio: str, tipo: str, candidatos: list[dict]) -> dict | None:
    if not candidatos:
        return None

    temporadas = temporadas_para_anio(anio)

    if not GEMINI_API_KEY:
        if temporadas:
            for c in candidatos:
                t = (c.get("title") or "").lower()
                if any(temp.lower() in t for temp in temporadas):
                    return c
        return candidatos[0]

    prompt = (
        f"Busco la foto completa de una camiseta de fútbol: equipo='{equipo}', "
        f"año/temporada buscada='{anio}', formatos relacionados={temporadas}, equipación='{tipo}'.\n"
        f"Elige el 'id' del candidato que mejor muestre la camiseta COMPLETA de frente.\n"
        f"Reglas estrictas:\n"
        f"1. Debe ser una camiseta completa (prenda entera visible), NUNCA un primer plano o zoom del escudo, "
        f"ni un detalle del cuello/manga, ni un escudo suelto, ni un balón, ni un jugador de cintura para arriba.\n"
        f"2. Que coincida con el equipo y, si es posible, con la temporada ({temporadas}) y equipación ({tipo}).\n"
        f"3. Devuelve siempre un 'id' de la lista.\n\n"
        f"Candidatos JSON:\n{json.dumps(candidatos, ensure_ascii=False)}\n\n"
        f"Responde SOLO con JSON válido: {{\"id\": 0}}"
    )

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=12,
        )
        r.raise_for_status()
        texto = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        elegido_id = json.loads(texto).get("id")
        if elegido_id is not None:
            for c in candidatos:
                if c["id"] == elegido_id:
                    return c
    except Exception:
        pass

    return candidatos[0]


def resolver_camisetas(equipo: str, anio: str, kit_type: str) -> list[dict]:
    tipo_code = mapear_tipo(kit_type)
    clave = f"{normalizar(equipo)}_{normalizar(anio)}_{tipo_code}"
    if clave in CACHE:
        return CACHE[clave]

    resultados: list[dict] = []
    resultados.extend(buscar_en_catalogo_local(equipo, anio, tipo_code))

    if not resultados and SERPER_API_KEY:
        tipo_en = {
            "home": "home kit",
            "away": "away kit",
            "third": "third kit",
            "gk": "goalkeeper kit",
        }.get(tipo_code, "home kit")

        temps = temporadas_para_anio(anio)
        consultas = []
        if temps:
            consultas.append(f"{equipo} {temps[0]} {tipo_en} full jersey")
            if len(temps) > 2:
                consultas.append(f"{equipo} {temps[2]} {tipo_en} full jersey")
        consultas.append(f"{equipo} {anio} {tipo_en} full shirt".strip())

        for q in consultas:
            candidatos = buscar_en_google_imagenes(q)
            if candidatos:
                elegido = elegir_con_ia(equipo, anio, tipo_code, candidatos)
                if elegido:
                    resultados.append({
                        "url": elegido["imageUrl"],
                        "label": elegido["title"] or f"{equipo.title()} {anio} ({tipo_code.upper()})".strip(),
                    })
                break

    resultados = resultados[:1]
    CACHE[clave] = resultados
    return resultados


@app.post("/api/admin/login")
def admin_login(payload: AdminLogin):
    if payload.password == ADMIN_PASSWORD:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")


@app.get("/api/buscar")
def buscar_camiseta(
    equipo: str = Query(..., min_length=1),
    anio: str = Query(""),
    tipo: str = Query("home"),
):
    lista = resolver_camisetas(equipo, anio, tipo)
    if not lista:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron resultados para '{equipo}' ({anio or 'cualquier año'}, {tipo}).",
        )
    primero = lista[0]
    return {
        "equipo": equipo.title(),
        "anio": anio or "Temporada",
        "tipo": mapear_tipo(tipo).upper(),
        "titulo": primero["label"],
        "imagen": primero["url"],
        "kits": lista,
    }


@app.get("/api/kit")
def buscar_kit_editor(
    team: str = Query(..., min_length=1),
    season: str = Query("2026/27"),
    kit_type: str = Query("1ª Equipación"),
):
    return {"kits": resolver_camisetas(team, season, kit_type)}


def load_orders() -> list[dict]:
    if not ORDERS_FILE.exists():
        return []
    try:
        return json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_orders(orders: list[dict]) -> None:
    ORDERS_FILE.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/orders")
def get_orders():
    return {"orders": load_orders()}


@app.post("/api/orders")
def create_order(order: OrderCreate):
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
def patch_order(order_id: str, payload: StatusUpdate):
    if payload.status not in ("En espera", "Vendida"):
        raise HTTPException(status_code=400, detail="Estado no válido")
    orders = load_orders()
    for o in orders:
        if o["id"] == order_id:
            o["status"] = payload.status
            save_orders(orders)
            return {"order": o}
    raise HTTPException(status_code=404, detail="Pedido no encontrado")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def servir_inicio():
    return FileResponse(str(BASE_DIR / "index.html"))
