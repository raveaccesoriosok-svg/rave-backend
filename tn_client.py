import asyncio
import httpx
from config import TN_API_BASE, TN_ACCESS_TOKEN, TN_STORE_ID, TN_USER_AGENT


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
    try:
        r = await get_client().get(f"/products/{product_id}/variants/{variant_id}")
        if r.status_code == 404:
            return None
        if not r.is_success:
            raise TiendanubeError(r.status_code, r.text)
        data = r.json()
        if not data.get("stock_management", False):
            return None
        return data.get("stock", None)
    except httpx.TimeoutException:
        raise TiendanubeError(504, "Timeout conectando con Tiendanube")


_sem = asyncio.Semaphore(10)


async def _get_variant_stock_throttled(product_id: int, variant_id: int) -> int | None:
    async with _sem:
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
    r = await get_client().post("/draft_orders", json=payload)
    if not r.is_success:
        raise TiendanubeError(r.status_code, r.text)
    data = r.json()
    checkout_url = data.get("checkout_url")
    if not checkout_url:
        raise TiendanubeError(502, "Tiendanube no devolvio checkout_url")
    return checkout_url
