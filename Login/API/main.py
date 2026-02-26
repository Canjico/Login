import os
import json
import io
import logging
import uuid
from datetime import datetime, timedelta, date, time

import re
import html
from typing import Any, Optional, Dict, List, Tuple
from html.parser import HTMLParser

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

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
# Chatbot: Leitura de fontes + Evidências (RAG leve)
# =========================

# Limites para não explodir memória/latência
CHAT_MAX_DOWNLOAD_BYTES = int(os.getenv("CHAT_MAX_DOWNLOAD_BYTES", "12000000"))  # 12 MB
CHAT_REQUEST_TIMEOUT = int(os.getenv("CHAT_REQUEST_TIMEOUT", "15"))             # segundos
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "600"))                      # resposta curta
CHAT_MAX_SOURCES = int(os.getenv("CHAT_MAX_SOURCES", "12"))                     # hard cap por edital

_STOPWORDS_PT = {
    "a","o","os","as","um","uma","uns","umas","de","da","do","das","dos","em","no","na","nos","nas",
    "para","por","com","sem","que","e","é","ser","se","ao","aos","à","às","ou","como","mais","menos",
    "sobre","entre","até","já","não","sim","qual","quais","quando","onde","quem","porque","porquê",
    "pra","pro","pelo","pela","pelos","pelas","sua","seu","suas","seus","minha","meu","meus","minhas"
}

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: List[str] = []

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return "\n".join(self._chunks)

def _html_to_text(raw_html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(raw_html)
    return stripper.get_text()

def _download_url(url: str, timeout: int, max_bytes: int) -> Tuple[bytes, str]:
    """Baixa uma URL com limite de tamanho. Retorna (bytes, content_type)."""
    headers = {
        "User-Agent": "AtimusBot/1.0 (+https://editais.atimus.agr.br)",
        "Accept": "*/*"
    }
    with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = bytearray()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(f"Arquivo grande demais (> {max_bytes} bytes).")
        return bytes(data), content_type

def _extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(out)

def _extract_docx_text(content: bytes) -> str:
    if DocxDocument is None:
        raise RuntimeError("python-docx não disponível no servidor.")
    doc = DocxDocument(io.BytesIO(content))
    parts: List[str] = []
    for p in doc.paragraphs:
        if p.text and p.text.strip():
            parts.append(p.text.strip())
    # Tabelas (muitos editais escondem info aqui)
    try:
        for t in doc.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
    except Exception:
        pass
    return "\n".join(parts)

def _safe_decode(content: bytes) -> str:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return content.decode(enc)
        except Exception:
            continue
    return content.decode("utf-8", errors="ignore")

def _infer_kind(url: str, content_type: str, declared_tipo: str | None = None) -> str:
    u = (url or "").lower()
    ct = (content_type or "").lower()
    tp = (declared_tipo or "").lower()

    if "pdf" in ct or u.endswith(".pdf") or "pdf" in tp:
        return "pdf"
    if "word" in ct or u.endswith(".docx") or "docx" in tp:
        return "docx"
    if "html" in ct or u.endswith(".html") or u.endswith(".htm") or "página" in tp or "pagina" in tp:
        return "html"
    if "text/plain" in ct or u.endswith(".txt"):
        return "text"
    return "bin"

def _tokenize_query(q: str) -> List[str]:
    q = (q or "").lower()
    q = re.sub(r"[^a-z0-9áàâãéêíóôõúç\s-]", " ", q, flags=re.IGNORECASE)
    toks = [t.strip() for t in q.split() if len(t.strip()) >= 3]
    return [t for t in toks if t not in _STOPWORDS_PT]

def _select_snippets(text: str, query: str, max_snippets: int = 3, snippet_len: int = 900) -> List[str]:
    if not text:
        return []
    toks = _tokenize_query(query)
    paras = [p.strip() for p in re.split(r"\n{2,}|\r\n\r\n", text) if p and p.strip()]
    if not paras:
        paras = [p.strip() for p in text.splitlines() if p.strip()]

    # Score por overlap simples (rápido e suficiente pro caso)
    scored: List[Tuple[int, str]] = []
    for p in paras:
        lp = p.lower()
        score = 0
        for t in toks:
            if t in lp:
                score += 1
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)

    out: List[str] = []
    candidates = [p for _, p in scored[: max_snippets * 2]] if scored else paras[:max_snippets]
    for p in candidates:
        s = re.sub(r"\s+", " ", p).strip()
        if len(s) > snippet_len:
            s = s[:snippet_len].rstrip() + "…"
        if s and s not in out:
            out.append(s)
        if len(out) >= max_snippets:
            break
    return out

def _escape(s: str) -> str:
    return html.escape(s or "", quote=True)

def _sources_details_html(sources: List[Dict[str, Any]]) -> str:
    ok = sum(1 for s in sources if s.get("status") == "ok")
    total = len(sources)
    items = []
    for s in sources[:25]:
        st = "✅" if s.get("status") == "ok" else "⚠️"
        nome = _escape(str(s.get("nome") or s.get("host") or "fonte"))
        url = _escape(str(s.get("url") or ""))
        kind = _escape(str(s.get("kind") or ""))
        extra = _escape(str(s.get("error") or ""))
        chars = s.get("chars")
        chars_txt = f" • {chars} chars" if isinstance(chars, int) else ""
        items.append(f"<li>{st} <a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">{nome}</a> <small>({kind}{chars_txt})</small>{(' — ' + extra) if extra else ''}</li>")
    inner = "<ul style=\"margin:8px 0 0 18px;\">" + "".join(items) + "</ul>" if items else ""
    return f"<details style=\"margin-top:10px;\"><summary>Fontes consultadas: {ok}/{total} acessadas</summary>{inner}</details>"

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
    """Chat contextual por edital.
    Regras:
    - Resposta curta (resumo).
    - Só afirma com evidência (CARD ou FONTE X).
    - Faz o melhor esforço para ACESSAR e LER todos os links em arquivos_json.
    """
    if not client:
        return {"reply": "Chat indisponível no momento. (Azure OpenAI não configurado)."}

    user_q = (msg.message or "").strip()
    if not user_q:
        return {"reply": "Digite uma pergunta objetiva (ex: 'Qual o prazo?' / 'Quem pode participar?')."}

    edital = db.query(Edital).filter(Edital.id == edital_id).first()
    if not edital:
        return {"reply": "Edital não encontrado."}

    # 1) Dados do card (às vezes já responde)
    try:
        card = json.loads(edital.json_data) if edital.json_data else {}
        if not isinstance(card, dict):
            card = {}
    except Exception:
        card = {}

    # 2) Lista de fontes (arquivos + página web)
    try:
        arquivos = json.loads(edital.arquivos_json) if edital.arquivos_json else []
        if not isinstance(arquivos, list):
            arquivos = []
    except Exception:
        arquivos = []

    # Compat: se não vier lista, usa pdf_url antigo
    if (not arquivos) and getattr(edital, "pdf_url", None):
        arquivos.append({"tipo": "Documento", "url": edital.pdf_url, "nome": "Edital Completo (PDF)"})

    # Hard cap de segurança (evita edital com 200 links travar o servidor)
    if len(arquivos) > CHAT_MAX_SOURCES:
        arquivos = arquivos[:CHAT_MAX_SOURCES]

    sources: List[Dict[str, Any]] = []
    extracted_by_idx: Dict[int, str] = {}

    for i, a in enumerate(arquivos, start=1):
        if not isinstance(a, dict):
            continue

        url = (a.get("url") or "").strip()
        nome = (a.get("nome") or a.get("host") or f"Fonte {i}").strip()
        declared_tipo = (a.get("tipo") or "").strip()

        if not url:
            sources.append({"idx": i, "nome": nome, "url": "", "kind": "unknown", "status": "error", "error": "URL vazia"})
            continue

        try:
            content, content_type = _download_url(url, timeout=CHAT_REQUEST_TIMEOUT, max_bytes=CHAT_MAX_DOWNLOAD_BYTES)
            kind = _infer_kind(url, content_type, declared_tipo)

            text_out = ""
            if kind == "pdf":
                text_out = _extract_pdf_text(content)
            elif kind == "docx":
                text_out = _extract_docx_text(content)
            elif kind == "html":
                raw = _safe_decode(content)
                text_out = _html_to_text(raw)
            elif kind == "text":
                text_out = _safe_decode(content)
            else:
                # Fonte binária não suportada -> registra acesso, mas sem texto.
                text_out = ""

            # Sanitiza tamanho pra não explodir memória
            if len(text_out) > 350_000:
                text_out = text_out[:350_000]

            sources.append({
                "idx": i,
                "nome": nome,
                "url": url,
                "kind": kind,
                "status": "ok",
                "content_type": content_type,
                "chars": len(text_out),
            })
            extracted_by_idx[i] = text_out

        except Exception as e:
            logger.error(f"[CHAT] Erro ao acessar fonte {url}: {e}")
            sources.append({
                "idx": i,
                "nome": nome,
                "url": url,
                "kind": "unknown",
                "status": "error",
                "error": str(e)[:180],
            })

    # 3) Monta contexto com evidências curtas
    card_context = {
        "titulo": card.get("titulo") or getattr(edital, "titulo", None),
        "instituicao": card.get("instituicao") or card.get("instituição") or "",
        "estado": card.get("estado") or "",
        "objetivo": card.get("objetivo") or card.get("descricao") or getattr(edital, "descricao", "") or "",
        "publico_alvo": card.get("publico_alvo") or "",
        "valor_total": card.get("valor_total") or "",
        "data_final_submissao": card.get("data_final_submissao") or (str(edital.data_final_submissao) if getattr(edital, "data_final_submissao", None) else None),
        "fluxo_continuo": card.get("fluxo_continuo") if "fluxo_continuo" in card else getattr(edital, "fluxo_continuo", None),
        "tags": card.get("tags") or [],
        "motivo_aprovacao": card.get("motivo_aprovacao") or "",
        "share_link": f"{FRONTEND_APP_URL}?id={edital.id}",
    }

    # Trechos por fonte (RAG leve)
    fontes_txt = []
    for s in sources:
        idx_ = s.get("idx")
        if not isinstance(idx_, int):
            continue
        txt = extracted_by_idx.get(idx_, "")
        snippets = _select_snippets(txt, user_q, max_snippets=3, snippet_len=900)
        # Se a fonte foi acessada mas não tem texto (binário), ainda registra
        fontes_txt.append({
            "idx": idx_,
            "nome": s.get("nome"),
            "url": s.get("url"),
            "kind": s.get("kind"),
            "status": s.get("status"),
            "snippets": snippets,
            "chars": s.get("chars"),
            "error": s.get("error"),
        })

    context_lines = []
    context_lines.append("DADOS DO CARD (já exibidos ao cliente):")
    context_lines.append(json.dumps(card_context, ensure_ascii=False, indent=2))

    context_lines.append("\nFONTES (trechos relevantes; use como evidência):")
    for f in fontes_txt:
        idx_ = f.get("idx")
        nome = f.get("nome") or f"Fonte {idx_}"
        url = f.get("url") or ""
        kind = f.get("kind") or "unknown"
        status_ = f.get("status")
        context_lines.append(f"\n[FONTE {idx_}] {nome} | {kind} | {url} | status={status_}")
        if status_ != "ok":
            context_lines.append(f"ERRO: {f.get('error')}")
            continue
        snips = f.get("snippets") or []
        if not snips:
            context_lines.append("(sem trechos extraídos)")
        else:
            for j, sn in enumerate(snips, start=1):
                context_lines.append(f"- trecho {j}: {sn}")

    context_blob = "\n".join(context_lines)

    system_prompt = (
        "Você é um assistente de editais.\n"
        "Regras obrigatórias:\n"
        "1) Seja curto: no máximo 10 linhas OU 6 bullets.\n"
        "2) Só afirme algo se estiver explicitamente nos DADOS DO CARD ou nos TRECHOS das FONTES.\n"
        "3) Quando citar informação importante (prazo, valor, quem pode participar, documentos, como se inscrever), indique a origem no final da frase: (CARD) ou (FONTE X).\n"
        "4) Se faltar evidência, diga 'Não encontrei no material disponível' e sugira o que verificar.\n"
        "5) Se a resposta já estiver no CARD, priorize.\n"
        "6) Não invente links, números, prazos ou critérios.\n"
        "Formato sugerido:\n"
        "- Resumo em bullets\n"
        "- Evidências (1 a 3 bullets curtas com (CARD)/(FONTE X))\n"
    )

    # 4) Chamada ao modelo
    try:
        completion = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Pergunta: {user_q}\n\n{context_blob}"},
            ],
            temperature=0.2,
            max_completion_tokens=CHAT_MAX_TOKENS
        )
        reply_txt = (completion.choices[0].message.content or "").strip()
        if not reply_txt:
            reply_txt = "Não consegui gerar uma resposta com segurança com base no material disponível."
    except Exception as e:
        logger.error(f"[CHAT] Falha ao consultar Azure OpenAI: {e}")
        return {"reply": "⚠️ Ocorreu um erro ao consultar a IA. Tente novamente em instantes."}

    # 5) Transparência: mostra fontes consultadas (colapsável) sem poluir a resposta
    details_html = _sources_details_html(sources) if sources else ""
    reply_html = _escape(reply_txt).replace("\n", "<br>")
    final_reply = reply_html + details_html

    return {
        "reply": final_reply,
        "sources": sources
    }

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
    
    # ==============================
    # KPIs base (Editais)
    # ==============================
    qtd_hoje = db.query(Edital).filter(Edital.created_at >= hoje_start, Edital.created_at <= hoje_end).count()
    qtd_ontem = db.query(Edital).filter(Edital.created_at >= ontem_start, Edital.created_at <= ontem_end).count()
    
    total_ativos = db.query(Edital).filter(
        or_(Edital.data_final_submissao >= hoje_date, Edital.fluxo_continuo == True)
    ).count()
    
    # Expiração (agora 15 dias)
    limite_15dias = hoje_date + timedelta(days=15)
    expirando_15 = db.query(Edital).filter(
        Edital.data_final_submissao >= hoje_date,
        Edital.data_final_submissao <= limite_15dias,
        Edital.fluxo_continuo == False
    ).count()

    # Mantém compat com frontend antigo (7 dias)
    limite_7dias = hoje_date + timedelta(days=7)
    expirando_7 = db.query(Edital).filter(
        Edital.data_final_submissao >= hoje_date,
        Edital.data_final_submissao <= limite_7dias,
        Edital.fluxo_continuo == False
    ).count()

    # ==============================
    # KPIs (Instituições / Fontes)
    # ==============================
    # "Captações Hoje" agora vira: instituições distintas capturadas hoje.
    # As instituições vêm do json_data (IA) quando disponível.
    import json as _json

    editais_hoje_objs = db.query(Edital).filter(
        Edital.created_at >= hoje_start,
        Edital.created_at <= hoje_end
    ).order_by(Edital.created_at.desc()).all()

    instituicoes_set = set()
    novos_editais_hoje = []
    for e in editais_hoje_objs:
        instituicao = None
        try:
            jd = _json.loads(e.json_data) if e.json_data else {}
            instituicao = (jd.get("instituicao") or jd.get("instituição") or jd.get("org") or "")
            if isinstance(instituicao, str):
                instituicao = instituicao.strip() or None
        except Exception:
            instituicao = None

        if instituicao:
            instituicoes_set.add(instituicao)

        novos_editais_hoje.append({
            "id": e.id,
            "titulo": e.titulo,
            "fonte": e.link_origem or "Desconhecida",
            "criado_em": e.created_at.isoformat() if e.created_at else None,
            "instituicao": instituicao
        })

    instituicoes_hoje = len(instituicoes_set)

    # Fontes monitoradas: "OK/TOTAL" por dia.
    # OK = fontes distintas registradas em auditoria_logs HOJE.
    # TOTAL = valor fixo (atualmente 42) - pode virar env depois.
    FONTES_TOTAL_DIARIAS = int(os.getenv("FONTES_TOTAL_DIARIAS", "42"))
    fontes_ok_hoje = db.query(AuditoriaLog.fonte).filter(
        AuditoriaLog.data_execucao >= hoje_start,
        AuditoriaLog.data_execucao <= hoje_end
    ).distinct().count()

    # Top fontes: as que mais captaram no total (mantém gráfico e legenda)
    fontes_distinct = db.query(AuditoriaLog.fonte).distinct().all()
    total_fontes = len(fontes_distinct)
    top_fontes = [f[0] for f in fontes_distinct[:3]]
    top_fontes_str = ", ".join([t for t in top_fontes if t]) + ("..." if len(fontes_distinct) > 3 else "")

    # ==============================
    # Dados para Gráficos
    # ==============================
    # Compat: gráfico antigo por auditoria_logs (qtd de novos editais)
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

    # Novo: gráfico 7 dias por instituições distintas capturadas/dia
    grafico_instituicoes_7d = []
    for i in range(6, -1, -1):
        d = hoje_date - timedelta(days=i)
        start_dt = datetime.combine(d, datetime.min.time())
        end_dt = datetime.combine(d, datetime.max.time())

        eds = db.query(Edital.json_data).filter(
            Edital.created_at >= start_dt,
            Edital.created_at <= end_dt
        ).all()

        inst_set = set()
        for (json_str,) in eds:
            try:
                jd = _json.loads(json_str) if json_str else {}
                inst = (jd.get("instituicao") or jd.get("instituição") or jd.get("org") or "")
                if isinstance(inst, str):
                    inst = inst.strip()
                if inst:
                    inst_set.add(inst)
            except Exception:
                continue

        grafico_instituicoes_7d.append({"data": d.strftime("%d/%m"), "qtd": len(inst_set)})
    
    dist_fontes = db.query(AuditoriaLog.fonte, func.sum(AuditoriaLog.quantidade_novos))\
                    .group_by(AuditoriaLog.fonte)\
                    .order_by(func.sum(AuditoriaLog.quantidade_novos).desc())\
                    .limit(5).all()
    
    grafico_fontes = [{"fonte": f[0] or "Desconhecida", "qtd": f[1] or 0} for f in dist_fontes]

    # ==============================
    # Alertas do Sistema
    # ==============================
    # Regras novas:
    # - Alertar editais novos de hoje (nome por nome) com detalhe verde.
    # - Alertar editais que vencem em até 15 dias (do 15º até o HOJE), com cor por urgência.
    alerts = []

    # 1) Novos editais hoje (nome por nome)
    for e in novos_editais_hoje[:25]:  # hard cap pra não estourar UI
        title = e.get("titulo") or f"Edital #{e.get('id')}"
        fonte = e.get("fonte") or "Desconhecida"
        alerts.append({
            "type": "green",
            "title": title,
            "desc": f"Novo hoje • {fonte}",
            "kind": "novo_edital",
            "id": e.get("id")
        })

    # 2) Expirando em até 15 dias (lista detalhada)
    expirando_lista = []
    expirando_objs = db.query(Edital).filter(
        Edital.fluxo_continuo == False,
        Edital.data_final_submissao != None,
        Edital.data_final_submissao >= hoje_date,
        Edital.data_final_submissao <= limite_15dias
    ).order_by(Edital.data_final_submissao.asc()).all()

    for e in expirando_objs[:50]:
        dias_restantes = (e.data_final_submissao - hoje_date).days
        expirando_lista.append({
            "id": e.id,
            "titulo": e.titulo,
            "data_final": str(e.data_final_submissao),
            "dias_restantes": dias_restantes
        })

        # Cor por urgência
        if dias_restantes <= 0:
            tipo = "red"
            desc = "Vence HOJE"
        elif dias_restantes <= 3:
            tipo = "red"
            desc = f"Vence em {dias_restantes} dia(s)"
        elif dias_restantes <= 7:
            tipo = "yellow"
            desc = f"Vence em {dias_restantes} dia(s)"
        else:
            tipo = "green"
            desc = f"Vence em {dias_restantes} dia(s)"

        alerts.append({
            "type": tipo,
            "title": e.titulo,
            "desc": desc,
            "kind": "expirando",
            "id": e.id,
            "dias_restantes": dias_restantes
        })

    # 3) Sanidade do scraping (alerta se zero logs em 24h)
    start_24h = datetime.utcnow() - timedelta(hours=24)
    recent_logs = db.query(AuditoriaLog).filter(AuditoriaLog.data_execucao >= start_24h).count()
    if recent_logs == 0:
        alerts.append({"type": "yellow", "title": "Nenhuma auditoria em 24h", "desc": "Verificar execução do scraping", "kind": "scraping"})

    return {
        "editais_hoje": qtd_hoje,
        "editais_ontem": qtd_ontem,
        "total_ativos": total_ativos,
        "expirando_7_dias": expirando_7,
        "expirando_15_dias": expirando_15,
        "captacoes_hoje": instituicoes_hoje,
        "instituicoes_hoje": instituicoes_hoje,
        "novos_editais_hoje": novos_editais_hoje,
        "expirando_lista": expirando_lista,
        "fontes_ok_hoje": fontes_ok_hoje,
        "fontes_total": FONTES_TOTAL_DIARIAS,
        "total_fontes": total_fontes,
        "top_fontes_nomes": top_fontes_str,
        "grafico_dias": grafico_dias,
        "grafico_instituicoes_7d": grafico_instituicoes_7d,
        "grafico_fontes": grafico_fontes,
        "alerts": alerts
    }

@app.post("/admin/auditoria/executar")
def executar_auditoria(audit_req: AuditoriaExecucao, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")

    janela_inicio = datetime.utcnow() - timedelta(hours=audit_req.horas_retroativas)

    # Busca editais reais para extrair detalhes
    novos_editais_objs = db.query(Edital).filter(
        Edital.created_at >= janela_inicio
    ).all()

    if not novos_editais_objs:
        log_vazio = AuditoriaLog(
            ciclo=audit_req.ciclo,
            fonte="Geral",
            quantidade_novos=0,
            detalhes_json=json.dumps({
                "msg": "Nenhum edital novo no período",
                "janela_inicio": janela_inicio.isoformat(),
                "horas_retroativas": audit_req.horas_retroativas
            }, ensure_ascii=False)
        )
        db.add(log_vazio)
        db.commit()
        return {"msg": "Auditoria executada. Nenhum edital novo encontrado.", "logs": []}

    UF_VALIDAS = {
        "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR","PE","PI",
        "RJ","RN","RS","RO","RR","SC","SP","SE","TO","BR"
    }
    INSTITUICOES_NACIONAIS = {
        "CNPQ","FINEP","CAPES","MCTI","MEC","MS","MDIC","BNDES","EMBRAPA","ANA","ANP","ANVISA","IBAMA",
        "BNB","BASA","BB","CAIXA","SERPRO","DATAPREV","FIOCRUZ"
    }

    def detectar_incertezas_local(jd: dict, fonte=None) -> list[dict]:
        if not isinstance(jd, dict):
            return []

        incertezas: list[dict] = []

        # se já vier do próprio JSON, respeita
        raw = jd.get("incertezas") or jd.get("campos_incertos")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("campo"):
                    incertezas.append({
                        "campo": str(item.get("campo")),
                        "valor": item.get("valor"),
                        "motivo": str(item.get("motivo") or "")
                    })

        def _norm(s):
            return (s or "").strip().upper()

        inst = _norm(jd.get("instituicao") or jd.get("instituição"))
        fonte_norm = _norm(fonte)

        estado = _norm(jd.get("estado"))
        if not estado:
            incertezas.append({"campo": "estado", "valor": None, "motivo": "UF não encontrada; campo vazio"})
        else:
            if estado not in UF_VALIDAS:
                incertezas.append({"campo": "estado", "valor": estado, "motivo": "Valor fora do conjunto de UFs conhecidas"})
            elif estado == "BR":
                parece_federal = any(k in inst for k in INSTITUICOES_NACIONAIS) or any(k in fonte_norm for k in INSTITUICOES_NACIONAIS)
                if not parece_federal:
                    incertezas.append({"campo": "estado", "valor": "BR", "motivo": "UF não identificada com segurança; 'BR' parece fallback"})

        data_final = jd.get("data_final_submissao")
        if isinstance(data_final, str) and data_final.strip().lower() == "indefinido":
            incertezas.append({"campo": "data_final_submissao", "valor": data_final, "motivo": "Prazo não encontrado com segurança"})
        elif data_final is None:
            incertezas.append({"campo": "data_final_submissao", "valor": None, "motivo": "Campo ausente"})

        valor = jd.get("valor_total")
        if isinstance(valor, str) and valor.strip().lower() == "indefinido":
            incertezas.append({"campo": "valor_total", "valor": valor, "motivo": "Valor total não encontrado com segurança"})

        # dedupe
        seen=set()
        uniq=[]
        for it in incertezas:
            key=(it.get('campo'), str(it.get('valor')), it.get('motivo'))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        return uniq

    # Agrupa por fonte em memória (python) para montar o JSON de detalhes
    from collections import defaultdict
    agrupamento: dict[str, list[dict]] = defaultdict(list)

    for ed in novos_editais_objs:
        fonte_chave = ed.link_origem if ed.link_origem else "Desconhecida"

        jd = {}
        try:
            jd = json.loads(ed.json_data) if ed.json_data else {}
        except Exception:
            jd = {}

        incertezas = jd.get("incertezas") or jd.get("campos_incertos")
        if not isinstance(incertezas, list):
            incertezas = detectar_incertezas_local(jd, fonte=fonte_chave)

        agrupamento[fonte_chave].append({
            "id": ed.id,
            "titulo": ed.titulo,
            "link": f"{FRONTEND_APP_URL}?id={ed.id}",
            "incertezas": incertezas
        })

    logs_criados = []
    for fonte_nome, lista_items in agrupamento.items():
        detalhes_obj = {
            "janela_inicio": janela_inicio.isoformat(),
            "horas_retroativas": audit_req.horas_retroativas,
            "editais": lista_items
        }

        novo_log = AuditoriaLog(
            ciclo=audit_req.ciclo,
            fonte=fonte_nome,
            quantidade_novos=len(lista_items),
            detalhes_json=json.dumps(detalhes_obj, ensure_ascii=False)
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
