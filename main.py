import json
import urllib.parse
import smtplib
import secrets
import html
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from google import genai
import os

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

# Credenciales SMTP
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "velaeduardo365@gmail.com"
SENDER_PASSWORD = "uaje tpew okyj oboz"

class UserAuth(BaseModel):
    email: str
    password: str
    name: Optional[str] = "Usuario"

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

def send_verification_email(recipient_email: str, verification_url: str) -> None:
    template_path = Path(__file__).resolve().parent / "templates" / "email_verification.html"
    html_content = template_path.read_text(encoding="utf-8").replace(
        "{{ verification_url }}",
        html.escape(verification_url, quote=True),
    )

    msg = MIMEMultipart('alternative')
    msg['From'] = f"Shoppy-AI <{SENDER_EMAIL}>"
    msg['To'] = recipient_email
    msg['Subject'] = "Verifica tu cuenta en Shoppy-AI"
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())

@app.get("/")
async def serve_frontend():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "API Activa. Coloca tu index.html en el directorio raíz."}

@app.post("/api/register")
async def register_user(
    user: UserAuth,
    request: Request,
    background_tasks: BackgroundTasks,
):
    email = user.email.strip().lower()
    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        existing = db.query(DBUser).filter(DBUser.email == email).first()

        if existing and existing.verified:
            raise HTTPException(
                status_code=400,
                detail="Este correo ya esta registrado y verificado. Inicia sesion.",
            )

        if existing:
            existing.verification_token = token
            message = "La cuenta ya existia sin verificar. Te hemos reenviado el correo."
        else:
            db.add(DBUser(
                email=email,
                name=user.name,
                password=user.password,
                verified=False,
                verification_token=token,
            ))
            message = f"Registro exitoso. Revisa tu bandeja de entrada en {email} para verificar tu cuenta."

        db.commit()
    finally:
        db.close()

    verification_url = f"{request.base_url}api/verify-email?token={token}"
    background_tasks.add_task(send_verification_email, email, verification_url)
    return {"status": "success", "message": message}

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