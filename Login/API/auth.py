# API/auth.py

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Dict

from fastapi import HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from passlib.context import CryptContext

# =========================
# Configurações JWT
# =========================

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8

# Não derruba a aplicação no import
JWT_SECRET = os.getenv("JWT_SECRET", "")

# Pepper para hash de token (Camada extra de segurança)
RESET_TOKEN_PEPPER = os.getenv("RESET_TOKEN_PEPPER", "")

# =========================
# Segurança
# =========================

# ATENÇÃO: Requer passlib==1.7.4 e bcrypt==3.2.2 para evitar 
# erro "AttributeError: module 'bcrypt' has no attribute '__about__'"
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)

bearer_scheme = HTTPBearer(auto_error=True)

# =========================
# Utilitários
# =========================

def _get_jwt_secret() -> str:
    """
    Retorna o segredo JWT.
    Lança erro apenas no uso, não no import.
    """
    if not JWT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET não configurado no ambiente"
        )
    return JWT_SECRET

# =========================
# Hash de senha
# =========================

def hash_senha(senha: str) -> str:
    """
    Gera hash bcrypt da senha.
    O passlib espera string, não bytes. Truncamos a string em 72 caracteres
    para evitar erros do algoritmo bcrypt com senhas longas.
    """
    senha_truncada = senha[:72]
    return pwd_context.hash(senha_truncada)

def verificar_senha(senha: str, senha_hash: str) -> bool:
    """
    Verifica se a senha confere com o hash.
    """
    senha_truncada = senha[:72]
    return pwd_context.verify(senha_truncada, senha_hash)

# =========================
# Token de Recuperação (Secure)
# =========================

def gerar_reset_token() -> str:
    """
    Gera um token seguro URL-safe (aleatório).
    """
    return secrets.token_urlsafe(32)

def hash_token(token: str) -> str:
    """
    Gera o hash SHA-256 do token + PEPPER para armazenamento seguro no banco.
    O pepper impede que ataques de rainbow table funcionem facilmente caso o banco vaze.
    """
    # Concatena o pepper ao token antes de hashar
    dados = token + RESET_TOKEN_PEPPER
    return hashlib.sha256(dados.encode()).hexdigest()

# =========================
# JWT
# =========================

def criar_token(dados: Dict) -> str:
    """
    Cria token JWT com expiração.
    """
    secret = _get_jwt_secret()

    payload = dados.copy()
    # Usando UTC Naive para compatibilidade com sistema legado/drivers simples
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload.update({"exp": expire})

    return jwt.encode(payload, secret, algorithm=ALGORITHM)

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
) -> Dict:
    """
    Dependency do FastAPI para recuperar usuário a partir do JWT.
    """
    secret = _get_jwt_secret()
    token = credentials.credentials

    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou expirado"
        )
