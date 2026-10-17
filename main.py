import asyncio
import base64
import json
import logging
import re
import urllib.parse
import secrets
from datetime import datetime, timezone
from pathlib import Path
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from google import genai
import os

logger = logging.getLogger(__name__)

# Base de datos SQLite con SQLAlchemy
from sqlalchemy import create_engine, Column, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./users.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class DBUser(Base):
    __tablename__ = "users"
    email = Column(String, primary_key=True, index=True)
    name = Column(String)
    password = Column(String)
    verified = Column(Boolean, default=False)
    verification_token = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

# Almacenamiento temporal en memoria; se pierde al reiniciar el proceso.
verification_codes_db = {}

APP_TITLE = "Shoppy-AI | Tu Asistente de Compras Inteligente"
APP_DESCRIPTION = (
    "Encuentra las mejores ofertas, compara precios en tiempo real y ahorra dinero "
    "con nuestra Inteligencia Artificial basada en eBay."
)
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title=APP_TITLE, description=APP_DESCRIPTION)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clave API segura mediante variable de entorno
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Clave de Brevo proporcionada mediante variables de entorno.
BREVO_API_KEY = (os.environ.get("BREVO_API_KEY") or "").strip()
BREVO_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"
VERIFICATION_SENDER_EMAIL = "velaeduardo365@gmail.com"
EBAY_CLIENT_ID = (os.environ.get("EBAY_CLIENT_ID") or "").strip()
EBAY_CLIENT_SECRET = (os.environ.get("EBAY_CLIENT_SECRET") or "").strip()
EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SERPAPI_KEY = (os.environ.get("SERPAPI_KEY") or "").strip()
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"

class UserAuth(BaseModel):
    email: str
    password: str
    name: Optional[str] = "Usuario"

class VerifyCodeRequest(BaseModel):
    email: str
    code: str

class ResendCodeRequest(BaseModel):
    email: str

class ProductSearchRequest(BaseModel):
    query: str

class DealOption(BaseModel):
    store: str
    price: float
    is_real_price: bool = False
    price_source: str = "Demostración"
    image_url: Optional[str] = None
    shipping_days: Optional[int]
    url: str
    condition: str = "Nuevo"
    title: str
    import_fee: Optional[float]
    price_history: List[float]

class DealResponse(BaseModel):
    user_original_query: str
    ai_parsed_intent: str
    max_price_filter: Optional[float]
    selected_store: str
    best_deal: DealOption
    average_market_price: float
    money_saved: float
    percentage_saved: float
    time_saved_minutes: float
    all_options: List[DealOption]

class AlertRequest(BaseModel):
    email: str
    product_title: str
    target_price: float
    current_price: float
    store: str

class AlertResponse(BaseModel):
    status: str
    message: str

def get_ebay_token() -> str:
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="La integración con eBay no está configurada. Define EBAY_CLIENT_ID y EBAY_CLIENT_SECRET.",
        )

    credentials = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
    ).decode("ascii")

    try:
        response = requests.post(
            EBAY_OAUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
    except requests.RequestException as error:
        logger.warning("Could not reach eBay OAuth API: %s", error)
        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con la autenticación de eBay.",
        ) from None

    if response.status_code != 200:
        logger.error("eBay OAuth rejected credentials with HTTP %s", response.status_code)
        raise HTTPException(
            status_code=502,
            detail="eBay rechazó las credenciales de API. Revisa EBAY_CLIENT_ID y EBAY_CLIENT_SECRET.",
        )

    try:
        access_token = response.json().get("access_token")
    except ValueError:
        logger.error("eBay OAuth returned invalid JSON")
        raise HTTPException(status_code=502, detail="eBay devolvió una respuesta de autenticación inválida.") from None
    except AttributeError:
        logger.error("eBay OAuth returned an unexpected JSON structure")
        raise HTTPException(status_code=502, detail="eBay devolvió una respuesta de autenticación inválida.") from None
    if not access_token:
        logger.error("eBay OAuth response did not contain an access token")
        raise HTTPException(status_code=502, detail="eBay no devolvió un token de acceso válido.")
    return access_token

def search_ebay_items(query: str, limit: int = 3) -> list[dict]:
    token = get_ebay_token()
    try:
        response = requests.get(
            EBAY_SEARCH_URL,
            params={"q": query, "limit": limit},
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
            },
            timeout=20,
        )
    except requests.RequestException as error:
        logger.warning("Could not reach eBay Browse API: %s", error)
        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con la búsqueda de eBay.",
        ) from None

    if response.status_code != 200:
        logger.error("eBay Browse API returned HTTP %s", response.status_code)
        if response.status_code in (401, 403):
            detail = "eBay rechazó el acceso a Browse API. Revisa las credenciales y permisos de la aplicación."
        else:
            detail = f"La búsqueda de eBay falló (HTTP {response.status_code})."
        raise HTTPException(status_code=502, detail=detail)

    try:
        items = response.json().get("itemSummaries", [])
    except ValueError:
        logger.error("eBay Browse API returned invalid JSON")
        raise HTTPException(status_code=502, detail="eBay devolvió resultados con formato inválido.") from None
    except AttributeError:
        logger.error("eBay Browse API returned an unexpected JSON structure")
        raise HTTPException(status_code=502, detail="eBay devolvió resultados con formato inválido.") from None
    if not isinstance(items, list):
        logger.error("eBay Browse API returned an invalid itemSummaries value")
        raise HTTPException(status_code=502, detail="eBay devolvió resultados con formato inválido.")
    return items

def ebay_items_to_deals(items: list[dict]) -> list[DealOption]:
    deals = []
    for item in items:
        item_price = item.get("price") or {}
        if item_price.get("currency") != "USD":
            continue
        try:
            price = float(item_price["value"])
        except (KeyError, TypeError, ValueError):
            continue

        shipping_days = None
        shipping_options = item.get("shippingOptions") or []
        if shipping_options:
            delivery_date = shipping_options[0].get("maxEstimatedDeliveryDate")
            if delivery_date:
                try:
                    delivery = datetime.fromisoformat(delivery_date.replace("Z", "+00:00"))
                    shipping_days = max(
                        0,
                        (delivery.date() - datetime.now(timezone.utc).date()).days,
                    )
                except ValueError:
                    logger.warning("eBay returned an invalid estimated delivery date")

        deals.append(DealOption(
            store="eBay",
            price=price,
            is_real_price=True,
            price_source="eBay Browse API",
            image_url=(item.get("image") or {}).get("imageUrl"),
            shipping_days=shipping_days,
            url=item.get("itemWebUrl") or "",
            condition=item.get("condition") or "Condición no especificada",
            title=item.get("title") or "Producto de eBay",
            import_fee=None,
            price_history=[],
        ))
    return deals

def parse_shopping_price(item: dict) -> Optional[float]:
    extracted_price = item.get("extracted_price")
    try:
        if extracted_price is not None:
            price = float(extracted_price)
        else:
            price_text = item.get("price")
            if not isinstance(price_text, str):
                return None
            numeric_text = re.sub(r"[^\d.,]", "", price_text)
            if not numeric_text:
                return None
            if "," in numeric_text and "." not in numeric_text:
                decimal_part = numeric_text.rsplit(",", 1)[-1]
                numeric_text = numeric_text.replace(",", ".") if len(decimal_part) == 2 else numeric_text.replace(",", "")
            else:
                numeric_text = numeric_text.replace(",", "")
            price = float(numeric_text)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None

def search_serpapi_items(query: str, limit: int = 5) -> list[dict]:
    if not SERPAPI_KEY:
        raise HTTPException(
            status_code=503,
            detail="La búsqueda web no está configurada. Define SERPAPI_KEY en las variables de entorno.",
        )

    try:
        response = requests.get(
            SERPAPI_SEARCH_URL,
            params={
                "engine": "google_shopping",
                "q": query,
                "hl": "es",
                "gl": "us",
                "api_key": SERPAPI_KEY,
            },
            timeout=20,
        )
    except requests.RequestException as error:
        logger.warning("Could not reach SerpApi (%s)", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con el buscador web. Intenta nuevamente.",
        ) from None

    if response.status_code != 200:
        logger.error("SerpApi rejected search with HTTP %s", response.status_code)
        if response.status_code in (401, 403):
            detail = "SerpApi rechazó la clave. Verifica que SERPAPI_KEY sea válida y esté activa."
        elif response.status_code == 429:
            detail = "Se alcanzó el límite de búsquedas de SerpApi. Intenta más tarde."
        else:
            detail = f"La búsqueda web falló (HTTP {response.status_code})."
        raise HTTPException(status_code=502, detail=detail)

    try:
        result = response.json()
    except ValueError:
        logger.error("SerpApi returned invalid JSON")
        raise HTTPException(status_code=502, detail="El buscador devolvió una respuesta inválida.") from None
    if not isinstance(result, dict):
        logger.error("SerpApi returned an unexpected response structure")
        raise HTTPException(status_code=502, detail="El buscador devolvió una respuesta inválida.")
    if result.get("error"):
        logger.error("SerpApi reported a search error")
        raise HTTPException(status_code=502, detail="SerpApi no pudo completar la búsqueda.")

    shopping_results = result.get("shopping_results", [])
    if not isinstance(shopping_results, list):
        logger.error("SerpApi returned invalid shopping_results")
        raise HTTPException(status_code=502, detail="El buscador devolvió resultados inválidos.")
    return shopping_results[:limit]

def serpapi_items_to_deals(items: list[dict]) -> list[DealOption]:
    deals = []
    for item in items:
        if not isinstance(item, dict):
            continue
        price = parse_shopping_price(item)
        source = item.get("source")
        title = item.get("title")
        link = item.get("link")
        if price is None or not isinstance(source, str) or not source.strip() or not isinstance(title, str) or not isinstance(link, str):
            continue
        deals.append(DealOption(
            store=source.strip(),
            price=price,
            is_real_price=True,
            price_source="Google Shopping vía SerpApi",
            image_url=item.get("thumbnail") if isinstance(item.get("thumbnail"), str) else None,
            shipping_days=None,
            url=link,
            condition="Nuevo",
            title=title,
            import_fee=None,
            price_history=[],
        ))
    return deals

def deal_matches_store(deal: DealOption, store_filter: str) -> bool:
    normalized_store = re.sub(r"[^a-z0-9]", "", deal.store.lower())
    normalized_filter = re.sub(r"[^a-z0-9]", "", store_filter.lower())
    return normalized_store == normalized_filter or normalized_store.startswith(normalized_filter)

def send_http_email_code(to_email: str, code: str) -> None:
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY no está configurada.")

    payload = {
        "sender": {
            "name": "Shoppy-AI Soporte",
            "email": VERIFICATION_SENDER_EMAIL,
        },
        "to": [{"email": to_email}],
        "subject": f"Tu código de verificación es {code}",
        "htmlContent": f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #09090b; color: #fff; padding: 30px; border-radius: 16px; max-width: 480px; margin: auto; border: 1px solid #27272a;">
            <div style="text-align: center; margin-bottom: 20px;">
                <span style="background: #2563eb; color: white; padding: 10px 14px; border-radius: 12px; font-weight: bold; font-size: 18px;">S</span>
                <h2 style="color: #ffffff; margin-top: 15px; font-size: 20px;">Verificación de Shoppy-AI</h2>
            </div>
            <p style="color: #a1a1aa; font-size: 14px; text-align: center;">Usa el siguiente código de 6 dígitos para activar tu cuenta:</p>
            <div style="font-size: 32px; font-weight: bold; background: #18181b; color: #3b82f6; padding: 15px; border-radius: 12px; text-align: center; letter-spacing: 6px; margin: 20px 0; border: 1px solid #27272a;">
                {code}
            </div>
            <p style="font-size: 12px; color: #71717a; text-align: center;">Si no solicitaste este código, puedes ignorar este mensaje de forma segura.</p>
        </div>
        """,
    }
    response = requests.post(
        BREVO_EMAIL_URL,
        json=payload,
        headers={
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
        },
        timeout=20,
    )
    if response.status_code not in (200, 201, 202):
        logger.error("Brevo rejected verification email with HTTP %s", response.status_code)
        if response.status_code in (401, 403):
            message = "Brevo rechazó BREVO_API_KEY. Comprueba que la clave sea válida y esté activa."
        elif response.status_code == 400:
            message = "Brevo rechazó los datos del correo. Verifica que el remitente esté autorizado en Brevo."
        elif response.status_code == 429:
            message = "Brevo limitó temporalmente los envíos. Espera un momento e inténtalo de nuevo."
        else:
            message = f"Brevo rechazó el envío (HTTP {response.status_code})."
        raise RuntimeError(message)

async def deliver_verification_code(email: str, code: str) -> None:
    if not BREVO_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="El servicio de correo no está configurado. Define BREVO_API_KEY en Render y reinicia el backend.",
        )

    try:
        await asyncio.to_thread(send_http_email_code, email, code)
    except requests.RequestException as error:
        logger.warning("Could not reach Brevo email API: %s", error)
        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con la API de Brevo. Intenta nuevamente más tarde.",
        ) from None
    except RuntimeError as error:
        logger.error("Brevo email delivery failed: %s", error)
        raise HTTPException(status_code=503, detail=str(error)) from None

@app.get("/")
async def serve_frontend():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

@app.post("/api/register")
async def register_user(
    user: UserAuth,
):
    email = user.email.strip().lower()
    code = str(secrets.randbelow(900000) + 100000)
    db = SessionLocal()
    try:
        existing = db.query(DBUser).filter(DBUser.email == email).first()

        if existing and existing.verified:
            raise HTTPException(
                status_code=409,
                detail="Ya existe una cuenta verificada con este correo. Inicia sesión.",
            )

        if existing:
            existing.verification_token = None
        else:
            db.add(DBUser(
                email=email,
                name=user.name,
                password=user.password,
                verified=False,
                verification_token=None,
            ))

        db.commit()
    finally:
        db.close()

    await deliver_verification_code(email, code)
    verification_codes_db[email] = code
    return {
        "status": "success",
        "success": True,
        "message": (
            "Ya existe una cuenta pendiente de verificación. Enviamos un nuevo código a tu correo."
            if existing
            else "Código de verificación enviado al correo electrónico."
        ),
        "account_exists": existing is not None,
    }

@app.post("/api/resend-code")
async def resend_verification_code(payload: ResendCodeRequest):
    email = payload.email.strip().lower()
    db = SessionLocal()
    try:
        user = db.query(DBUser).filter(DBUser.email == email).first()
        if not user or user.verified:
            raise HTTPException(
                status_code=400,
                detail="No hay una cuenta pendiente de verificación para este correo.",
            )
    finally:
        db.close()

    code = str(secrets.randbelow(900000) + 100000)
    await deliver_verification_code(email, code)
    verification_codes_db[email] = code
    return {
        "status": "success",
        "success": True,
        "message": "Código reenviado. Revisa tu correo y la carpeta de spam.",
    }

@app.post("/api/verify-code")
async def verify_code(payload: VerifyCodeRequest):
    email = payload.email.strip().lower()
    stored_code = verification_codes_db.get(email)

    if not stored_code or stored_code != payload.code:
        raise HTTPException(status_code=400, detail="Código incorrecto o expirado")

    db = SessionLocal()
    try:
        user = db.query(DBUser).filter(DBUser.email == email).first()
        if not user:
            raise HTTPException(status_code=400, detail="No existe una cuenta pendiente de verificación")

        user.verified = True
        user.verification_token = None
        db.commit()
    finally:
        db.close()

    del verification_codes_db[email]
    return {
        "status": "success",
        "success": True,
        "message": "¡Cuenta verificada con éxito!",
    }

@app.get("/api/verify-email", response_class=HTMLResponse)
async def verify_email(token: str = Query(...)):
    db = SessionLocal()
    user = db.query(DBUser).filter(DBUser.verification_token == token).first()
    
    if not user:
        db.close()
        return """
        <html><body style="background:#000;color:#fff;text-align:center;font-family:sans-serif;padding-top:80px;">
        <div style="max-width:400px;margin:auto;background:#18181b;border:1px solid #27272a;padding:40px;border-radius:16px;">
            <h2 style="color:#ef4444;margin-top:0;">Enlace inválido o expirado</h2>
            <p style="color:#a1a1aa;font-size:14px;">El token de verificación no es válido o ya fue utilizado anteriormente.</p>
        </div>
        </body></html>
        """
    
    user.verified = True
    user.verification_token = None
    db.commit()
    db.close()

    return """
    <html><body style="background:#09090b;color:#f4f4f5;text-align:center;font-family:sans-serif;padding-top:80px;">
        <div style="max-width:400px;margin:auto;background:#18181b;border:1px solid #27272a;padding:40px;border-radius:16px;">
            <h2 style="color:#10b981;margin-top:0;">¡Cuenta Verificada con Éxito!</h2>
            <p style="color:#a1a1aa;font-size:14px;">Tu correo ha sido confirmado correctamente. Ya puedes iniciar sesión en Shoppy-AI.</p>
            <a href="/" style="color:#3b82f6;">Ir al inicio de sesión</a>
        </div>
    </body></html>
    """

@app.post("/api/login")
async def login_user(user: UserAuth):
    email = user.email.strip().lower()
    db = SessionLocal()
    stored = db.query(DBUser).filter(DBUser.email == email).first()
    db.close()

    if not stored:
        raise HTTPException(status_code=400, detail="Usuario no encontrado. Regístrate primero.")
    
    if stored.password != user.password:
        raise HTTPException(status_code=400, detail="Contraseña incorrecta.")
    
    if not stored.verified:
        raise HTTPException(status_code=400, detail="Debes verificar tu correo electrónico haciendo clic en el enlace que te enviamos antes de iniciar sesión.")
    
    return {"status": "success", "message": f"¡Bienvenido de nuevo, {stored.name}!", "email": email, "name": stored.name}

def analyze_query_with_gemini(raw_query: str) -> dict:
    if not client:
        return {"optimized_query": raw_query, "max_price": None, "condition": "Nuevo"}
    
    prompt = f"""
    Eres un asistente de compras experto. Analiza la solicitud del usuario: "{raw_query}"
    Devuelve un JSON estricto con:
    - "optimized_query": Término clave corto para buscar.
    - "max_price": Número flotante del presupuesto máximo o null.
    - "condition": "Nuevo" o "Usado" (por defecto "Nuevo").
    Responde ÚNICAMENTE con el JSON válido.
    """
    try:
        response = client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        print(f"Error con Gemini: {e}")
        return {"optimized_query": raw_query, "max_price": None, "condition": "Nuevo"}

@app.post("/api/search-ebay")
async def search_ebay(data: ProductSearchRequest):
    query = data.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Escribe un producto para buscar en eBay.")

    items = await asyncio.to_thread(search_ebay_items, query, 3)
    deals_by_url = {
        item.url: item
        for item in ebay_items_to_deals(items)
    }
    return {
        "success": True,
        "results": [
            {
                "titulo": deal.title,
                "precio": deal.price,
                "moneda": "USD",
                "condicion": deal.condition,
                "link": deal.url,
                "imagen": (raw_item.get("image") or {}).get("imageUrl"),
            }
            for raw_item in items
            if (deal := deals_by_url.get(raw_item.get("itemWebUrl") or ""))
        ],
    }

@app.post("/api/search-web")
async def search_entire_web(data: ProductSearchRequest):
    query = data.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Escribe un producto para buscar en la web.")

    items = await asyncio.to_thread(search_serpapi_items, query, 5)
    return {
        "success": True,
        "results": [
            {
                "titulo": deal.title,
                "precio": deal.price,
                "tienda": deal.store,
                "link": deal.url,
                "imagen": deal.image_url,
            }
            for deal in serpapi_items_to_deals(items)
        ],
    }

@app.post("/api/search-deals", response_model=DealResponse)
async def search_deals(query: str, store_filter: str = "Todas"):
    parsed = analyze_query_with_gemini(query)
    opt_query = parsed.get("optimized_query", query)
    max_price = parsed.get("max_price")
    condition = parsed.get("condition", "Nuevo")
    
    normalized_store_filter = store_filter.strip().lower()
    all_deals = []

    if SERPAPI_KEY:
        shopping_items = await asyncio.to_thread(search_serpapi_items, opt_query, 10)
        all_deals.extend(serpapi_items_to_deals(shopping_items))

    include_ebay = normalized_store_filter in ("todas", "ebay")
    if include_ebay and (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET or normalized_store_filter == "ebay"):
        ebay_items = await asyncio.to_thread(search_ebay_items, opt_query, 3)
        all_deals.extend(ebay_items_to_deals(ebay_items))

    if not SERPAPI_KEY and not (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) and normalized_store_filter in ("todas", "amazon", "walmart"):
        encoded_q = urllib.parse.quote(opt_query)
        base_p = 45.0 if not max_price else max_price * 0.95
        demo_deals = [
            DealOption(
                store="Amazon",
                price=round(base_p, 2),
                shipping_days=2,
                url=f"https://www.amazon.com/s?k={encoded_q}",
                condition=condition,
                title=f"Amazon Global: {opt_query}",
                import_fee=round(base_p * 0.12, 2),
                price_history=[round(base_p * 1.25, 2), round(base_p * 1.15, 2), round(base_p * 1.2, 2), round(base_p * 1.1, 2), round(base_p * 1.05, 2), round(base_p, 2)],
            ),
            DealOption(
                store="Walmart",
                price=round(base_p * 1.08, 2),
                shipping_days=3,
                url=f"https://www.walmart.com/search?q={encoded_q}",
                condition=condition,
                title=f"Walmart Store Deal: {opt_query}",
                import_fee=round(base_p * 0.14, 2),
                price_history=[round(base_p * 1.3, 2), round(base_p * 1.25, 2), round(base_p * 1.2, 2), round(base_p * 1.15, 2), round(base_p * 1.1, 2), round(base_p * 1.08, 2)],
            ),
        ]
        all_deals.extend(demo_deals)

    if normalized_store_filter and normalized_store_filter != "todas":
        all_deals = [d for d in all_deals if deal_matches_store(d, normalized_store_filter)]

    unique_deals = []
    seen_urls = set()
    for deal in all_deals:
        if deal.url not in seen_urls:
            seen_urls.add(deal.url)
            unique_deals.append(deal)
    all_deals = unique_deals

    if max_price:
        all_deals = [d for d in all_deals if d.price <= max_price]

    if not all_deals:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron ofertas que coincidan con los filtros.",
        )

    real_prices = [d.price for d in all_deals if d.is_real_price]
    prices = real_prices or [d.price for d in all_deals]
    avg_price = sum(prices) / len(prices)
    sorted_deals = sorted(all_deals, key=lambda x: x.price)
    best_deal = sorted_deals[0]
    
    ref_price = max(prices) if len(prices) > 1 else best_deal.price
    money_saved = round(ref_price - best_deal.price, 2) if len(prices) > 1 else 0
    percentage_saved = round((money_saved / ref_price) * 100, 1) if ref_price > 0 else 0.0

    return DealResponse(
        user_original_query=query,
        ai_parsed_intent=opt_query,
        max_price_filter=max_price,
        selected_store=store_filter,
        best_deal=best_deal,
        average_market_price=round(avg_price, 2),
        money_saved=money_saved,
        percentage_saved=percentage_saved,
        time_saved_minutes=14.95,
        all_options=all_deals
    )

@app.post("/api/set-alert", response_model=AlertResponse)
async def set_price_alert(alert: AlertRequest):
    return AlertResponse(status="success", message=f"¡Alerta activada correctamente para {alert.email}!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)