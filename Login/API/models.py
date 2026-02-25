# API/models.py

from sqlalchemy import Column, Integer, String, Date, Text, DateTime, Boolean
from sqlalchemy.dialects.mssql import NVARCHAR
from datetime import datetime

from .database import Base

# =========================
# Models (Production Ready)
# =========================

class Edital(Base):
    __tablename__ = "editais"

    id = Column(Integer, primary_key=True, index=True)
    
    # Identidade e Controle
    hash_edital = Column(String(255), unique=True, index=True, nullable=True)
    titulo = Column(String(400), index=True)
    
    # Conteúdo
    link_origem = Column(String(255), index=True, default="Desconhecida") # Antigo 'fonte'
    descricao = Column(Text, nullable=True)
    
    # JSON armazenado como texto
    json_data = Column(Text)
    
    # Lista de arquivos serializada em JSON
    arquivos_json = Column(Text)
    
    # Datas e Prazos
    data_final_submissao = Column(Date, nullable=True)
    fluxo_continuo = Column(Boolean, default=False)
    
    # Legado / Auxiliar
    pdf_url = Column(String(500), nullable=True)

    # Auditoria de Entrada (Alinhado com Scraper)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditoriaLog(Base):
    __tablename__ = "auditoria_logs"

    id = Column(Integer, primary_key=True, index=True)
    data_execucao = Column(DateTime, default=datetime.utcnow)
    
    # Ciclo de auditoria: '06h', '10h', '18h' ou 'Manual'
    ciclo = Column(String(50), nullable=False)
    
    # Fonte auditada (ex: FINEP)
    fonte = Column(String(255), nullable=False)
    
    # Quantidade de novos editais detectados neste ciclo
    quantidade_novos = Column(Integer, default=0)
    
    # JSON com IDs ou detalhes para rastreabilidade
    detalhes_json = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(NVARCHAR(255), nullable=False, unique=True, index=True)
    senha_hash = Column(NVARCHAR(255), nullable=False)
    role = Column(NVARCHAR(50), nullable=False)


class Cliente(Base):
    __tablename__ = "clientes"

    id = Column(Integer, primary_key=True)
    nome = Column(NVARCHAR(255), nullable=False)
    # index=True explicitamente para buscas rápidas no login
    email = Column(NVARCHAR(255), nullable=False, unique=True, index=True)
    celular = Column(NVARCHAR(50))
    
    # index=True adicionado para otimizar validação de unicidade no cadastro
    cnpj = Column(NVARCHAR(20), nullable=False, unique=True, index=True)
    
    # Autenticação e Segurança
    senha_hash = Column(NVARCHAR(255), nullable=False)
    email_verificado = Column(Boolean, default=False)
    
    # Verificação de E-mail
    email_token = Column(NVARCHAR(255), unique=True, index=True)
    
    # Removido timezone=True para compatibilidade com DATETIME do SQL Server
    email_token_expiration = Column(DateTime, nullable=True)

    # Recuperação de Senha (HARDENING)
    reset_token_hash = Column(NVARCHAR(255), nullable=True, index=True)
    reset_token_expiration = Column(DateTime, nullable=True)

    # Consentimento e Sessão
    contato_ok = Column(Boolean, default=False)
    politica_ok = Column(Boolean, default=False)
    
    # Token de sessão (cookie)
    token = Column(NVARCHAR(255), unique=True, index=True)
    token_expiration = Column(DateTime, nullable=True)
    
    # Sempre UTC para consistência (Naive datetime)
    criado_em = Column(DateTime, default=datetime.utcnow)
