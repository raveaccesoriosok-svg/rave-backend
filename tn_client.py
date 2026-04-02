import asyncio
import time
import httpx
from config import TN_API_BASE, TN_ACCESS_TOKEN, TN_STORE_ID, TN_USER_AGENT

# ── Stock cache (variant_id → (stock_value, expires_at)) ─────────────────────
_stock_cache: dict[str, tuple[int | None, float]] = {}
STOCK_CACHE_TTL = 300  # 5 minutos


def _cache_get(variant_id: int) -> tuple[bool, int | None]:
    key = str(variant_id)
    entry = _stock_cache.get(key)
    if entry and time.monotonic() < entry[1]:
        return True, entry[0]
    return False, None


def _cache_set(variant_id: int, stock: int | None):
    _stock_cache[str(variant_id)] = (stock, time.monotonic() + STOCK_CACHE_TTL)


class TiendanubeError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


# Cliente HTTP compartido (se inicializa en el lifespan de FastAPI)
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialized")
    return _client


def create_client() -> httpx.AsyncClient:
    global _client
    _client = httpx.AsyncClient(
        base_url=f"{TN_API_BASE}/{TN_STORE_ID}",
        headers={
            "Authentication": f"bearer {TN_ACCESS_TOKEN}",
            "User-Agent": TN_USER_AGENT,
            "Content-Type": "application/json",
        },
        timeout=30.0,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    return _client


async def close_client():
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def get_variant_stock(product_id: int, variant_id: int) -> int | None:
    """
    Devuelve el stock de una variante.
    - int  → stock disponible (puede ser 0 = agotado)
    - None → stock no gestionado (asumir disponible)
    """
    hit, cached = _cache_get(variant_id)
    if hit:
        return cached

    try:
        r = await get_client().get(f"/products/{product_id}/variants/{variant_id}")
        if r.status_code == 404:
            _cache_set(variant_id, None)
            return None
        if r.status_code == 429:
            raise TiendanubeError(429, "Rate limit de Tiendanube alcanzado, reintentá en unos segundos")
        if not r.is_success:
            raise TiendanubeError(r.status_code, r.text)
        data = r.json()
        # Si la variante está explícitamente marcada como no disponible, tratar como agotado
        if not data.get("available", True):
            _cache_set(variant_id, 0)
            return 0
        if not data.get("stock_management", False):
            _cache_set(variant_id, None)
            return None

        # Multi-ubicación: si hay stock_locations, usar el stock del depósito principal
        stock_locations = data.get("stock_locations", [])
        if stock_locations:
            import json as _json
            print(f"[stock_locations] variant={variant_id} locations={_json.dumps(stock_locations)}")
            # Buscar el depósito principal (main=True o el primero)
            main_loc = next(
                (loc for loc in stock_locations if loc.get("main") or loc.get("is_main") or loc.get("is_default")),
                stock_locations[0]
            )
            stock = main_loc.get("stock", data.get("stock", None))
        else:
            stock = data.get("stock", None)

        _cache_set(variant_id, stock)
        return stock
    except httpx.TimeoutException:
        raise TiendanubeError(504, "Timeout conectando con Tiendanube")


_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(5)
    return _sem


async def _get_variant_stock_throttled(product_id: int, variant_id: int) -> int | None:
    async with _get_sem():
        return await get_variant_stock(product_id, variant_id)


async def get_many_variant_stocks(
    items: list[dict],
) -> dict[str, int | None]:
    """
    Recibe lista de {tn_product_id, tn_variant_id} y devuelve
    {str(variant_id): stock_or_null} con máximo 10 requests simultáneas.
    """
    tasks = [
        _get_variant_stock_throttled(item["tn_product_id"], item["tn_variant_id"])
        for item in items
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    stock_map = {}
    for item, result in zip(items, results):
        vid = str(item["tn_variant_id"])
        if isinstance(result, Exception):
            stock_map[vid] = None  # en caso de error individual, no bloqueamos
        else:
            stock_map[vid] = result
    return stock_map


async def create_draft_order(items: list[dict]) -> str:
    """
    Crea un draft order con los items seleccionados.
    Devuelve la checkout_url para redirigir al usuario.
    items = [{tn_variant_id: int, quantity: int}]
    """
    # Re-validar stock fresco antes de crear el pedido (ignora cache)
    for item in items:
        pid = item.get("tn_product_id")
        vid = item["tn_variant_id"]
        if pid:
            # 1) Verificar si el producto está disponible a nivel producto
            try:
                rp = await get_client().get(f"/products/{pid}")
                if rp.is_success:
                    prod_data = rp.json()
                    if not prod_data.get("available", True):
                        raise TiendanubeError(422, "Producto agotado o no disponible")
            except TiendanubeError:
                raise
            except Exception:
                pass  # si falla la verificación de producto, seguimos con la de variante

            # 2) Verificar stock de la variante (invalidar cache para datos frescos)
            _stock_cache.pop(str(vid), None)
            fresh_stock = await get_variant_stock(pid, vid)
            if fresh_stock is not None and fresh_stock == 0:
                raise TiendanubeError(422, "Producto agotado o no disponible")

    products = [
        {"variant_id": item["tn_variant_id"], "quantity": item.get("quantity", 1)}
        for item in items
    ]
    payload = {
        "products": products,
        "contact_name": "Cliente",
        "contact_lastname": "Web",
        "contact_email": "checkout@raveaccesorios.com.ar",
        "payment_status": "unpaid",
    }
    for attempt in range(3):
        r = await get_client().post("/draft_orders", json=payload)
        if r.status_code == 429:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
                continue
            raise TiendanubeError(429, "Tiendanube rate limit: demasiadas solicitudes, reintentá en unos segundos")
        if not r.is_success:
            raise TiendanubeError(r.status_code, r.text)
        data = r.json()
        checkout_url = data.get("checkout_url")
        if not checkout_url:
            raise TiendanubeError(502, "Tiendanube no devolvio checkout_url")
        return checkout_url
