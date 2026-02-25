import os
import json
import io
import logging
import uuid
from datetime import datetime, timedelta, date, time

import requests
from fastapi import FastAPI, Depends, HTTPException, Body, Request, status, Cookie, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, text, inspect, func, extract, cast, Date
from sqlalchemy.exc import OperationalError, IntegrityError
from pydantic import BaseModel, Field
from openai import AzureOpenAI
from PyPDF2 import PdfReader

from .database import engine, get_db
from .models import Base, Edital, User, Cliente, AuditoriaLog
from .auth import verificar_senha, hash_senha, criar_token, get_current_user, gerar_reset_token, hash_token
from .email_service import enviar_email_verificacao, enviar_email_recuperacao

# =========================
# App Initialization
# =========================
app = FastAPI(title="API de Editais + Chatbot")

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

# =========================
# Configurações de Ambiente (URLs)
# =========================
FRONTEND_LOGIN_URL = os.getenv("FRONTEND_LOGIN_URL", "http://127.0.0.1:5500/index.html")
FRONTEND_APP_URL = os.getenv("FRONTEND_APP_URL", "http://127.0.0.1:5500/editais.html")

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://login.atimus.agr.br",
        "https://editais.atimus.agr.br",
        "https://admin.atimus.agr.br",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Azure OpenAI Config
# =========================
AZURE_API_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_ENDPOINT = "https://thall-mj3g48g6-eastus2.services.ai.azure.com"
DEPLOYMENT_NAME = "gpt-5.2-chat"
API_VERSION = "2024-12-01-preview"

client: AzureOpenAI | None = None
if AZURE_API_KEY:
    client = AzureOpenAI(
        api_key=AZURE_API_KEY,
        api_version=API_VERSION,
        azure_endpoint=AZURE_ENDPOINT
    )
    logger.info("Azure OpenAI configurado com sucesso.")
else:
    logger.warning("Azure OpenAI NÃO configurado.")

# =========================
# Startup & Auto-Migration
# =========================
@app.on_event("startup")
def startup():
    try:
        # 1. Garante que tabelas existam
        Base.metadata.create_all(bind=engine)
        logger.info("Banco de dados: Estrutura base verificada.")
        
        # 2. AUTO-MIGRATION: Adiciona colunas novas se faltarem
        inspector = inspect(engine)
        
        with engine.connect() as conn:
            # MIGRATION: Tabela Editais (Alinhamento com Scrapers)
            if inspector.has_table("editais"):
                columns_edi = [c["name"] for c in inspector.get_columns("editais")]
                
                if "created_at" not in columns_edi:
                    logger.info("MIGRATION: Editais -> created_at...")
                    conn.execute(text("ALTER TABLE editais ADD created_at DATETIME DEFAULT CURRENT_TIMESTAMP"))
                
                if "link_origem" not in columns_edi:
                    logger.info("MIGRATION: Editais -> link_origem...")
                    conn.execute(text("ALTER TABLE editais ADD link_origem VARCHAR(255) DEFAULT 'Desconhecida'"))
                
                if "hash_edital" not in columns_edi:
                    logger.info("MIGRATION: Editais -> hash_edital...")
                    conn.execute(text("ALTER TABLE editais ADD hash_edital VARCHAR(255) NULL"))
                
                if "descricao" not in columns_edi:
                    logger.info("MIGRATION: Editais -> descricao...")
                    conn.execute(text("ALTER TABLE editais ADD descricao VARCHAR(MAX) NULL"))

                if "fluxo_continuo" not in columns_edi:
                    logger.info("MIGRATION: Editais -> fluxo_continuo...")
                    conn.execute(text("ALTER TABLE editais ADD fluxo_continuo BIT DEFAULT 0"))

            # MIGRATION: Tabela Clientes
            if inspector.has_table("clientes"):
                columns_cli = [c["name"] for c in inspector.get_columns("clientes")]
                if "reset_token_hash" not in columns_cli:
                    conn.execute(text("ALTER TABLE clientes ADD reset_token_hash VARCHAR(255) NULL"))
                if "reset_token_expiration" not in columns_cli:
                    conn.execute(text("ALTER TABLE clientes ADD reset_token_expiration DATETIME NULL"))
                if "token" not in columns_cli:
                    conn.execute(text("ALTER TABLE clientes ADD token VARCHAR(255) NULL"))
                if "token_expiration" not in columns_cli:
                    conn.execute(text("ALTER TABLE clientes ADD token_expiration DATETIME NULL"))
            
            conn.commit()

        logger.info("Banco de dados: Migrações verificadas.")
    except Exception as e:
        logger.error(f"Erro crítico ao iniciar banco: {e}")

# =========================
# Models (Pydantic)
# =========================
class LoginAdmin(BaseModel):
    email: str
    senha: str

class LoginCliente(BaseModel):
    email: str
    senha: str

class ChatMessage(BaseModel):
    message: str

class CadastroCliente(BaseModel):
    nome: str
    email: str
    senha: str = Field(..., min_length=6, max_length=64)
    celular: str
    cnpj: str
    contato_ok: bool
    politica_ok: bool

class EsqueciSenhaRequest(BaseModel):
    email: str

class RedefinirSenhaRequest(BaseModel):
    token: str
    nova_senha: str = Field(..., min_length=6, max_length=64)

class AdminSetup(BaseModel):
    email: str
    senha: str
    secret_key: str

class AuditoriaExecucao(BaseModel):
    ciclo: str = Field(..., description="Ex: '06h', '10h', '18h' ou 'Manual'")
    horas_retroativas: int = Field(default=8, description="Quantas horas olhar para trás para contar novos editais")

# =========================
# Utils
# =========================
def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str).split("T")[0], "%Y-%m-%d").date()
    except ValueError:
        return None

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    try:
        user, domain = email.split("@")
        if len(user) <= 2:
            return f"{user}***@{domain}"
        return f"{user[:2]}***@{domain}"
    except Exception:
        return "***@***"

def force_naive_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt

def simular_envio_email(email: str, token: str, tipo: str = "verificacao"):
    if tipo == "verificacao":
        base_api_url = os.getenv("BASE_API_URL", "http://127.0.0.1:8000")
        link = f"{base_api_url}/cliente/verificar-email?token={token}"
    else:
        if "?" in FRONTEND_LOGIN_URL:
            link = f"{FRONTEND_LOGIN_URL}&reset_token={token}"
        else:
            link = f"{FRONTEND_LOGIN_URL}?reset_token={token}"
    
    logger.info("====================================================")
    logger.info(f"[SIMULAÇÃO DE EMAIL - {tipo.upper()}] Para: {mask_email(email)}")
    logger.info(f"[AÇÃO] Clique no link: {link}")
    logger.info("====================================================")

# =========================
# Middleware de Log
# =========================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"{request.method} {request.url}")
    response = await call_next(request)
    return response

# =========================
# Routes Public / System
# =========================
@app.get("/ping")
def ping():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    status_report = {"status": "ok", "components": {}}
    try:
        db.execute(text("SELECT 1"))
        status_report["components"]["database"] = "healthy"
    except Exception as e:
        logger.error(f"Health Check Falhou (DB): {e}")
        status_report["status"] = "degraded"
        status_report["components"]["database"] = str(e)

    if client:
        status_report["components"]["openai"] = "configured"
    else:
        status_report["components"]["openai"] = "not_configured"

    if status_report["status"] != "ok":
        return JSONResponse(status_code=503, content=status_report)
        
    return status_report

@app.get("/")
def root():
    return {"msg": "API Atimus Online."}

@app.get("/editais")
def listar_editais(db: Session = Depends(get_db)):
    resultados = db.query(Edital).all()
    frontend_url = FRONTEND_APP_URL

    lista = []
    for r in resultados:
        try:
            json_data = json.loads(r.json_data) if r.json_data else {}
        except Exception:
            json_data = {}

        try:
            arquivos = json.loads(r.arquivos_json) if r.arquivos_json else []
        except Exception:
            arquivos = []

        if not arquivos and r.pdf_url:
            arquivos.append({
                "nome": "Edital Completo (PDF)",
                "url": r.pdf_url
            })

        lista.append({
            "id": r.id,
            "titulo": r.titulo,
            "fonte": r.link_origem if r.link_origem else "Desconhecida",
            "criado_em": r.created_at.isoformat() if r.created_at else None,
            "json_data": json_data,
            "arquivos_json": arquivos,
            "data_final_submissao": str(r.data_final_submissao) if r.data_final_submissao else None,
            "share_link": f"{frontend_url}?id={r.id}",
            "fluxo_continuo": r.fluxo_continuo
        })

    return JSONResponse(content=lista)

# =========================
# Fluxo Cliente: Auth & Cadastro
# =========================
@app.get("/cliente/me")
def cliente_me(cliente_token: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    if not cliente_token:
        return JSONResponse(status_code=401, content={"logado": False, "msg": "Cookie não encontrado"})
    
    cliente = db.query(Cliente).filter(Cliente.token == cliente_token).first()
    
    if not cliente:
        return JSONResponse(status_code=401, content={"logado": False, "msg": "Sessão inválida"})
    
    if not cliente.email_verificado:
        return JSONResponse(status_code=403, content={"logado": False, "msg": "Email não verificado"})
    
    now = datetime.utcnow()
    token_expiration_naive = force_naive_utc(cliente.token_expiration)

    if token_expiration_naive and now > token_expiration_naive:
        return JSONResponse(status_code=401, content={"logado": False, "msg": "Sessão expirada"})
    
    if token_expiration_naive and (token_expiration_naive - now).days < 5:
        cliente.token_expiration = now + timedelta(days=30)
        db.commit()
    
    return {"logado": True, "nome": cliente.nome, "id": cliente.id, "redirect": FRONTEND_APP_URL}

@app.post("/cliente/cadastro")
def cadastro_cliente(dados: CadastroCliente, db: Session = Depends(get_db)):
    existente = db.query(Cliente).filter(or_(Cliente.email == dados.email, Cliente.cnpj == dados.cnpj)).first()
    if existente:
        return JSONResponse(status_code=400, content={"detail": "E-mail ou CNPJ já cadastrados. Tente fazer login."}) 

    verificacao_token = str(uuid.uuid4())
    
    novo_cliente = Cliente(
        nome=dados.nome,
        email=dados.email,
        senha_hash=hash_senha(dados.senha),
        celular=dados.celular,
        cnpj=dados.cnpj,
        contato_ok=dados.contato_ok,
        politica_ok=dados.politica_ok,
        email_verificado=False,
        email_token=verificacao_token,
        email_token_expiration=datetime.utcnow() + timedelta(hours=72)
    )
    db.add(novo_cliente)
    db.commit()

    enviado = enviar_email_verificacao(dados.email, verificacao_token)
    if not enviado:
        simular_envio_email(dados.email, verificacao_token, "verificacao")

    logger.info(f"Novo cadastro iniciado: {mask_email(dados.email)}")
    return {"sucesso": True, "msg": "Cadastro realizado! Verifique seu e-mail para ativar a conta."}

@app.post("/cliente/login")
def login_cliente(dados: LoginCliente, db: Session = Depends(get_db)):
    cliente = db.query(Cliente).filter(Cliente.email == dados.email).first()
    if not cliente or not verificar_senha(dados.senha, cliente.senha_hash):
        return JSONResponse(status_code=401, content={"detail": "E-mail ou senha incorretos."}) 
    
    if not cliente.email_verificado:
        return JSONResponse(status_code=403, content={"detail": "Seu e-mail ainda não foi verificado. Verifique sua caixa de entrada."}) 

    sessao_token = str(uuid.uuid4())
    cliente.token = sessao_token
    cliente.token_expiration = datetime.utcnow() + timedelta(days=30)
    
    if getattr(cliente, 'reset_token_hash', None):
        cliente.reset_token_hash = None
        cliente.reset_token_expiration = None

    db.commit()

    logger.info(f"Login sucesso: Cliente {cliente.id} ({mask_email(cliente.email)})")
    
    response = JSONResponse(content={
        "redirect": FRONTEND_APP_URL, 
        "sucesso": True, 
        "msg": "Login realizado com sucesso"
    })
    
    response.set_cookie(
        key="cliente_token",
        value=sessao_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=30 * 24 * 60 * 60,
        path="/"
    )

    return response

@app.get("/cliente/verificar-email")
def verificar_email(token: str, db: Session = Depends(get_db)):
    cliente = db.query(Cliente).filter(Cliente.email_token == token).first()
    
    if not cliente:
        return JSONResponse(status_code=400, content={"detail": "Token inválido ou não encontrado."}) 
    
    if cliente.email_verificado:
        return RedirectResponse(url=f"{FRONTEND_LOGIN_URL}?verificado=true")

    expiration_naive = force_naive_utc(cliente.email_token_expiration)
    if expiration_naive and datetime.utcnow() > expiration_naive:
        return JSONResponse(status_code=400, content={"detail": "Este link de verificação expirou."}) 
    
    cliente.email_verificado = True,
    cliente.email_token = None
    cliente.email_token_expiration = None
    db.commit()

    return RedirectResponse(url=f"{FRONTEND_LOGIN_URL}?verificado=true")

@app.post("/cliente/esqueci-senha")
def esqueci_senha(req: EsqueciSenhaRequest, db: Session = Depends(get_db)):
    cliente = db.query(Cliente).filter(Cliente.email == req.email).first()
    msg_padrao = "Se este e-mail estiver cadastrado, você receberá um link de recuperação."

    if cliente:
        now = datetime.utcnow()
        exp_naive = force_naive_utc(getattr(cliente, 'reset_token_expiration', None))
        
        if exp_naive and now < exp_naive:
             return {"msg": msg_padrao}

        cliente.reset_token_hash = None
        cliente.reset_token_expiration = None

        raw_token = gerar_reset_token()
        hashed_token = hash_token(raw_token)

        cliente.reset_token_hash = hashed_token
        cliente.reset_token_expiration = now + timedelta(minutes=30)
        db.commit()

        enviado = enviar_email_recuperacao(cliente.email, raw_token)
        if not enviado:
             simular_envio_email(cliente.email, raw_token, "recuperacao")

    return {"msg": msg_padrao}

@app.post("/cliente/redefinir-senha")
def redefinir_senha(req: RedefinirSenhaRequest, db: Session = Depends(get_db)):
    if len(req.nova_senha) < 6 or len(req.nova_senha) > 64:
        raise HTTPException(status_code=400, detail="A senha deve ter entre 6 e 64 caracteres.")

    hashed_input = hash_token(req.token)
    
    cliente = db.query(Cliente).filter(
        Cliente.reset_token_hash == hashed_input,
        Cliente.reset_token_expiration > datetime.utcnow()
    ).first()
    
    if not cliente:
        return JSONResponse(status_code=400, content={"detail": "Link inválido ou expirado. Solicite um novo."}) 
    
    cliente.senha_hash = hash_senha(req.nova_senha)
    cliente.token = None
    cliente.token_expiration = None
    cliente.reset_token_hash = None
    cliente.reset_token_expiration = None
    db.commit()

    return {"msg": "Senha redefinida com sucesso. Faça login agora."}

# =========================
# Chat
# =========================
@app.post("/chat")
async def chat_search(msg: ChatMessage, db: Session = Depends(get_db)):
    if not msg.message or not msg.message.strip():
        return {"reply": "Me diga algo para eu procurar (ex: Inovação, Saúde, Finep)."}
    user_text = msg.message.strip()
    termos = [t for t in user_text.split() if len(t) > 2]
    if not termos:
        return {"reply": "Use palavras mais específicas como Inovação, Tecnologia, Saúde."}
    
    filtros = [
        or_(
            Edital.titulo.ilike(f"%{t}%"), 
            Edital.json_data.ilike(f"%{t}%"),
            Edital.descricao.ilike(f"%{t}%") 
        ) for t in termos
    ]
    resultados = db.query(Edital).filter(or_(*filtros)).limit(5).all()
    if not resultados:
        return {"reply": "Não encontrei editais com esses termos. Tente algo mais geral."}
    return {"reply": "Encontrei estes editais:", "options": [{"id": r.id, "titulo": r.titulo} for r in resultados]}

@app.post("/chat/edital/{edital_id}")
async def chat_edital(edital_id: int, msg: ChatMessage, db: Session = Depends(get_db)):
    if not client:
        return {"reply": "Chat indisponível no momento. (Azure OpenAI não configurado)."}
    edital = db.query(Edital).filter(Edital.id == edital_id).first()
    if not edital:
        return {"reply": "Edital não encontrado."}
    try:
        arquivos = json.loads(edital.arquivos_json) if edital.arquivos_json else []
    except Exception:
        arquivos = []
    if not arquivos and edital.pdf_url:
        arquivos.append({"url": edital.pdf_url})
    pdf_urls = [a.get("url") for a in arquivos if a.get("url", "").lower().endswith(".pdf")]
    if not pdf_urls:
        return {"reply": "Este edital não possui PDF."}
    texto = ""
    for url in pdf_urls:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                reader = PdfReader(io.BytesIO(r.content))
                for page in reader.pages:
                    texto += page.extract_text() or ""
        except Exception as e:
            logger.error(f"Erro ao ler PDF {url}: {e}")
    if not texto.strip():
        return {"reply": "Não consegui extrair texto do edital."}
    texto = texto[:200_000]
    response = client.chat.completions.create(
        model=DEPLOYMENT_NAME,
        messages=[
            {"role": "system", "content": "Você é um assistente especializado em editais. Responda apenas com base no texto fornecido."}, 
            {"role": "user", "content": f"Pergunta: {msg.message}\n\n{texto}"}
        ],
        max_completion_tokens=2048
    )
    return {"reply": response.choices[0].message.content}

# =========================
# Admin Routes
# =========================
@app.post("/admin/login")
def login_admin(login: LoginAdmin = Body(...), db: Session = Depends(get_db)):
    logger.info(f"[ADMIN LOGIN] Tentativa para: {login.email}")
    user = db.query(User).filter(User.email == login.email).first()
    
    if not user:
        logger.warning(f"[ADMIN LOGIN] Falha: Usuário {login.email} não encontrado.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
    
    if not verificar_senha(login.senha, user.senha_hash):
        logger.warning(f"[ADMIN LOGIN] Falha: Senha incorreta para {login.email}.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
        
    token = criar_token({"sub": user.email, "role": user.role})
    logger.info(f"[ADMIN LOGIN] Sucesso para: {login.email}")
    return {"access_token": token, "token_type": "bearer"}

@app.post("/admin/setup-user")
def setup_admin_user(dados: AdminSetup, db: Session = Depends(get_db)):
    SETUP_SECRET = os.getenv("ADMIN_SETUP_SECRET", "admin123_setup_key")
    
    if dados.secret_key != SETUP_SECRET:
        logger.warning(f"[ADMIN SETUP] Tentativa não autorizada com chave: {dados.secret_key}")
        raise HTTPException(status_code=403, detail="Chave de setup inválida")

    user = db.query(User).filter(User.email == dados.email).first()
    novo_hash = hash_senha(dados.senha)
    
    if user:
        user.senha_hash = novo_hash
        user.role = "admin"
        action = "atualizado"
    else:
        user = User(email=dados.email, senha_hash=novo_hash, role = "admin")
        db.add(user)
        action = "criado"
    
    db.commit()
    logger.info(f"[ADMIN SETUP] Usuário {dados.email} {action} com sucesso.")
    return {"msg": f"Admin {dados.email} {action} com sucesso.", "status": "ok"}

@app.get("/admin/protected")
def admin_area(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso restrito")
    return {"msg": f"Bem-vindo, {user['sub']}!"}

@app.post("/admin/editais")
def criar_edital(dados: dict = Body(...), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    attachments = dados.get("attachments", [])
    conteudo = dados.copy()
    conteudo.pop("attachments", None)
    
    novo = Edital(
        titulo=dados.get("titulo", "Sem Título"),
        link_origem=dados.get("fonte", "Manual"), 
        data_final_submissao=parse_date(dados.get("data_final_submissao")),
        pdf_url=attachments[0].get("url") if attachments else "",
        json_data=json.dumps(conteudo),
        arquivos_json=json.dumps(attachments),
        created_at=datetime.utcnow()
    )
    db.add(novo)
    db.commit()
    db.refresh(novo)
    return {"msg": "Edital criado com sucesso", "id": novo.id, "share_link": f"{FRONTEND_APP_URL}?id={novo.id}"}

@app.put("/admin/editais/{edital_id}")
def atualizar_edital(edital_id: int, dados: dict = Body(...), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    edital = db.query(Edital).filter(Edital.id == edital_id).first()
    if not edital:
        raise HTTPException(status_code=404, detail="Edital não encontrado")
    attachments = dados.get("attachments", [])
    conteudo = dados.copy()
    conteudo.pop("attachments", None)
    edital.titulo = dados.get("titulo", edital.titulo)
    edital.link_origem = dados.get("fonte", edital.link_origem)
    edital.data_final_submissao = parse_date(dados.get("data_final_submissao"))
    if attachments:
        edital.pdf_url = attachments[0].get("url")
    edital.json_data = json.dumps(conteudo)
    edital.arquivos_json = json.dumps(attachments)
    db.commit()
    return {"msg": "Edital atualizado com sucesso"}

@app.delete("/admin/editais/{edital_id}")
def excluir_edital(edital_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    edital = db.query(Edital).filter(Edital.id == edital_id).first()
    if not edital:
        raise HTTPException(status_code=404, detail="Edital não encontrado")
    
    db.delete(edital)
    db.commit()
    return {"msg": "Edital excluído com sucesso"}

# =========================
# AUDITORIA E ANALYTICS (DASHBOARD)
# =========================

@app.get("/admin/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    hoje_date = datetime.utcnow().date()
    hoje_start = datetime.combine(hoje_date, datetime.min.time())
    hoje_end = datetime.combine(hoje_date, datetime.max.time())
    
    ontem_date = hoje_date - timedelta(days=1)
    ontem_start = datetime.combine(ontem_date, datetime.min.time())
    ontem_end = datetime.combine(ontem_date, datetime.max.time())
    
    # 1. KPIs
    qtd_hoje = db.query(Edital).filter(Edital.created_at >= hoje_start, Edital.created_at <= hoje_end).count()
    qtd_ontem = db.query(Edital).filter(Edital.created_at >= ontem_start, Edital.created_at <= ontem_end).count()
    
    total_ativos = db.query(Edital).filter(
        or_(Edital.data_final_submissao >= hoje_date, Edital.fluxo_continuo == True)
    ).count()
    
    limite_7dias = hoje_date + timedelta(days=7)
    expirando = db.query(Edital).filter(
        Edital.data_final_submissao >= hoje_date,
        Edital.data_final_submissao <= limite_7dias
    ).count()

    captacoes_hoje = db.query(AuditoriaLog).filter(
        AuditoriaLog.data_execucao >= hoje_start,
        AuditoriaLog.data_execucao <= hoje_end
    ).count()

    fontes_distinct = db.query(AuditoriaLog.fonte).distinct().all()
    total_fontes = len(fontes_distinct)
    top_fontes = [f[0] for f in fontes_distinct[:3]]
    top_fontes_str = ", ".join([t for t in top_fontes if t]) + ("..." if len(fontes_distinct) > 3 else "")

    # 2. Dados para Gráficos
    grafico_dias = []
    for i in range(6, -1, -1):
        d = hoje_date - timedelta(days=i)
        start_dt = datetime.combine(d, datetime.min.time())
        end_dt = datetime.combine(d, datetime.max.time())
        
        qtd = db.query(func.sum(AuditoriaLog.quantidade_novos)).filter(
            AuditoriaLog.data_execucao >= start_dt,
            AuditoriaLog.data_execucao <= end_dt
        ).scalar()
        
        grafico_dias.append({"data": d.strftime("%d/%m"), "qtd": qtd or 0})
    
    dist_fontes = db.query(AuditoriaLog.fonte, func.sum(AuditoriaLog.quantidade_novos))\
                    .group_by(AuditoriaLog.fonte)\
                    .order_by(func.sum(AuditoriaLog.quantidade_novos).desc())\
                    .limit(5).all()
    
    grafico_fontes = [{"fonte": f[0] or "Desconhecida", "qtd": f[1] or 0} for f in dist_fontes]

    # 3. Alertas do Sistema
    alerts = []
    amanha = hoje_date + timedelta(days=1)
    vencem_amanha = db.query(Edital).filter(Edital.data_final_submissao == amanha).count()
    if vencem_amanha > 0:
        alerts.append({"type": "red", "title": f"{vencem_amanha} editais vencem amanhã", "desc": "Requer ação urgente"})
    
    start_24h = datetime.utcnow() - timedelta(hours=24)
    recent_logs = db.query(AuditoriaLog).filter(AuditoriaLog.data_execucao >= start_24h).count()
    if recent_logs == 0:
        alerts.append({"type": "yellow", "title": "Nenhuma captação em 24h", "desc": "Verificar conexão com fontes"})

    if qtd_hoje > 0:
         alerts.append({"type": "green", "title": f"{qtd_hoje} novos editais captados", "desc": "Hoje"})

    return {
        "editais_hoje": qtd_hoje,
        "editais_ontem": qtd_ontem,
        "total_ativos": total_ativos,
        "expirando_7_dias": expirando,
        "captacoes_hoje": captacoes_hoje,
        "total_fontes": total_fontes,
        "top_fontes_nomes": top_fontes_str,
        "grafico_dias": grafico_dias,
        "grafico_fontes": grafico_fontes,
        "alerts": alerts
    }

@app.post("/admin/auditoria/executar")
def executar_auditoria(audit_req: AuditoriaExecucao, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")

    janela_inicio = datetime.utcnow() - timedelta(hours=audit_req.horas_retroativas)
    
    # Busca Editais Reais para extrair detalhes
    novos_editais_objs = db.query(Edital).filter(
        Edital.created_at >= janela_inicio
    ).all()

    if not novos_editais_objs:
        log_vazio = AuditoriaLog(
            ciclo=audit_req.ciclo,
            fonte="Geral",
            quantidade_novos=0,
            detalhes_json=json.dumps({"msg": "Nenhum edital novo no período", "janela_inicio": janela_inicio.isoformat()})
        )
        db.add(log_vazio)
        db.commit()
        return {"msg": "Auditoria executada. Nenhum edital novo encontrado.", "logs": []}

    # Agrupa por fonte em memória (python) para montar o JSON de detalhes
    from collections import defaultdict
    agrupamento = defaultdict(list)
    
    for ed in novos_editais_objs:
        fonte_chave = ed.link_origem if ed.link_origem else "Desconhecida"
        agrupamento[fonte_chave].append({
            "id": ed.id,
            "titulo": ed.titulo,
            "link": f"{FRONTEND_APP_URL}?id={ed.id}"
        })

    logs_criados = []
    for fonte_nome, lista_items in agrupamento.items():
        novo_log = AuditoriaLog(
            ciclo=audit_req.ciclo,
            fonte=fonte_nome,
            quantidade_novos=len(lista_items),
            detalhes_json=json.dumps(lista_items) # Salva a lista real
        )
        db.add(novo_log)
        logs_criados.append({"fonte": fonte_nome, "qtd": len(lista_items)})
    
    db.commit()
    return {"msg": "Auditoria executada com sucesso.", "resultados": logs_criados}

@app.get("/admin/auditoria/relatorio")
def relatorio_auditoria(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    logs = db.query(AuditoriaLog).order_by(AuditoriaLog.data_execucao.desc()).limit(100).all()
    
    resultado = []
    for log in logs:
        status_txt = "Sucesso"
        if log.quantidade_novos == 0:
            status_txt = "Parcial"
        
        detalhes_parseados = []
        try:
            if log.detalhes_json:
                detalhes_parseados = json.loads(log.detalhes_json)
        except:
            pass

        resultado.append({
            "id": log.id,
            "data": log.data_execucao.isoformat(),
            "ciclo": log.ciclo,
            "fonte": log.fonte,
            "quantidade": log.quantidade_novos,
            "status": status_txt,
            "detalhes": detalhes_parseados,
            "acao": "Ver Detalhes"
        })
    
    return resultado