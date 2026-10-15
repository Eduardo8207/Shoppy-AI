import asyncio
import json
import logging
import urllib.parse
import secrets
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
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

app = FastAPI(title="AI Shopping Deal Finder - Cloud Ready")

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

class UserAuth(BaseModel):
    email: str
    password: str
    name: Optional[str] = "Usuario"

class VerifyCodeRequest(BaseModel):
    email: str
    code: str

class ResendCodeRequest(BaseModel):
    email: str

class DealOption(BaseModel):
    store: str
    price: float
    shipping_days: int
    url: str
    condition: str = "Nuevo"
    title: str
    import_fee: float
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
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "API Activa. Coloca tu index.html en el directorio raíz."}

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

@app.post("/api/search-deals", response_model=DealResponse)
async def search_deals(query: str, store_filter: str = "Todas"):
    parsed = analyze_query_with_gemini(query)
    opt_query = parsed.get("optimized_query", query)
    max_price = parsed.get("max_price")
    condition = parsed.get("condition", "Nuevo")
    
    encoded_q = urllib.parse.quote(opt_query)
    base_p = 45.0 if not max_price else max_price * 0.95
    
    all_deals = [
        DealOption(
            store="Amazon", 
            price=round(base_p, 2), 
            shipping_days=2, 
            url=f"https://www.amazon.com/s?k={encoded_q}", 
            condition=condition,
            title=f"Amazon Global: {opt_query}",
            import_fee=round(base_p * 0.12, 2),
            price_history=[round(base_p * 1.25, 2), round(base_p * 1.15, 2), round(base_p * 1.2, 2), round(base_p * 1.1, 2), round(base_p * 1.05, 2), round(base_p, 2)]
        ),
        DealOption(
            store="eBay", 
            price=round(base_p * 0.88, 2), 
            shipping_days=4, 
            url=f"https://www.ebay.com/sch/i.html?_nkw={encoded_q}", 
            condition=condition,
            title=f"eBay Verified Seller: {opt_query}",
            import_fee=round(base_p * 0.10, 2),
            price_history=[round(base_p * 1.1, 2), round(base_p * 1.05, 2), round(base_p * 0.98, 2), round(base_p * 0.95, 2), round(base_p * 0.9, 2), round(base_p * 0.88, 2)]
        ),
        DealOption(
            store="Walmart", 
            price=round(base_p * 1.08, 2), 
            shipping_days=3, 
            url=f"https://www.walmart.com/search?q={encoded_q}", 
            condition=condition,
            title=f"Walmart Store Deal: {opt_query}",
            import_fee=round(base_p * 0.14, 2),
            price_history=[round(base_p * 1.3, 2), round(base_p * 1.25, 2), round(base_p * 1.2, 2), round(base_p * 1.15, 2), round(base_p * 1.1, 2), round(base_p * 1.08, 2)]
        ),
    ]
    
    if store_filter and store_filter != "Todas":
        filtered = [d for d in all_deals if d.store.lower() == store_filter.lower()]
        if filtered:
            all_deals = filtered

    if max_price:
        all_deals = [d for d in all_deals if d.price <= max_price]

    prices = [d.price for d in all_deals]
    avg_price = sum(prices) / len(prices)
    sorted_deals = sorted(all_deals, key=lambda x: x.price)
    best_deal = sorted_deals[0]
    
    ref_price = max(prices) if len(prices) > 1 else best_deal.price * 1.2
    money_saved = round(ref_price - best_deal.price, 2)
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