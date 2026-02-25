# API/database.py

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

# =========================
# Configuração do Banco
# =========================

DATABASE_URL = os.getenv("DATABASE_URL")
print("DATABASE_URL =", DATABASE_URL)


if not DATABASE_URL:
    # Não derruba o app no import.
    # O erro real vai aparecer quando tentar usar o banco.
    DATABASE_URL = ""

# =========================
# Engine
# =========================

engine: Engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True
)

# =========================
# Session
# =========================

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    future=True
)

# =========================
# Base dos Models
# =========================

Base = declarative_base()

# =========================
# Dependency (FastAPI)
# =========================

def get_db():
    """
    Dependency padrão do FastAPI para obter sessão do banco.
    Garante abertura e fechamento correto da conexão.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
