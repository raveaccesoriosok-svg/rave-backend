from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import tn_client
from config import ALLOWED_ORIGINS


# ── Lifespan: crea y cierra el cliente HTTP ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    tn_client.create_client()
    yield
    await tn_client.close_client()


app = FastAPI(title="RAVE Accesorios – Tiendanube Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS] if ALLOWED_ORIGINS != "*" else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── Modelos ───────────────────────────────────────────────────────────────────
class StockItem(BaseModel):
    tn_product_id: int
    tn_variant_id: int


class StockRequest(BaseModel):
    items: list[StockItem]


class CheckoutItem(BaseModel):
    tn_variant_id: int
    quantity: int = 1


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/stock")
async def check_stock(body: StockRequest):
    """
    Devuelve el stock actual de cada variante enviada.
    Respuesta: {"stock": {"variant_id": cantidad_o_null}}
    null = sin gestión de stock (asumir disponible)
    0    = agotado
    """
    if not body.items:
        return {"stock": {}}

    try:
        stock_map = await tn_client.get_many_variant_stocks(
            [item.model_dump() for item in body.items]
        )
    except tn_client.TiendanubeError as e:
        raise HTTPException(status_code=502, detail=e.message)

    return {"stock": stock_map}


@app.post("/checkout")
async def create_checkout(body: CheckoutRequest):
    """
    Crea un draft order en Tiendanube con los items seleccionados
    y devuelve la URL de checkout para redirigir al usuario.
    """
    if not body.items:
        raise HTTPException(status_code=400, detail="No hay items seleccionados")

    try:
        checkout_url = await tn_client.create_draft_order(
            [item.model_dump() for item in body.items]
        )
    except tn_client.TiendanubeError as e:
        raise HTTPException(status_code=502, detail=e.message)

    return {"checkout_url": checkout_url}
