# email_service.py

import os
import logging
from azure.communication.email import EmailClient

logger = logging.getLogger("api.email")

# =========================
# Configurações ACS
# =========================

# ATENÇÃO: NUNCA commitar chaves reais aqui. 
# A Connection String deve vir das Variáveis de Ambiente (App Service Configuration ou .env local).
AZURE_ACS_CONNECTION_STRING = os.getenv("AZURE_ACS_CONNECTION_STRING")

# O remetente deve ser o endereço provisionado no Azure ou domínio verificado
# Ex: DoNotReply@<guid>.azurecomm.net ou noreply@atimus.agr.br
SENDER_EMAIL = os.getenv("AZURE_ACS_SENDER_ADDRESS")

# URL base da API para compor o link de verificação
# Em produção: https://api.atimus.agr.br (ou URL do App Service)
# Local: http://127.0.0.1:8000
BASE_API_URL = os.getenv("BASE_API_URL", "http://127.0.0.1:8000")

# URL do Frontend de Login para links de recuperação
FRONTEND_LOGIN_URL = os.getenv("FRONTEND_LOGIN_URL", "http://127.0.0.1:5500/index.html")

def enviar_email_verificacao(destinatario: str, token: str) -> bool:
    """
    Envia o e-mail de verificação usando Azure Communication Services.
    Retorna True se enviado, False se as chaves não estiverem configuradas ou houver erro.
    """
    if not AZURE_ACS_CONNECTION_STRING or not SENDER_EMAIL:
        logger.warning("AZURE_ACS_CONNECTION_STRING ou SENDER_EMAIL não configurados. E-mail real ignorado.")
        return False

    try:
        client = EmailClient.from_connection_string(AZURE_ACS_CONNECTION_STRING)
        
        # Garante que a URL não termine com barra para evitar // no link
        base_url = BASE_API_URL.rstrip('/')
        link_verificacao = f"{base_url}/cliente/verificar-email?token={token}"
        
        logger.info(f"Iniciando envio de e-mail de verificação para {destinatario} via ACS...")

        message = {
            "senderAddress": SENDER_EMAIL,
            "recipients":  {
                "to": [{"address": destinatario}]
            },
            "content": {
                "subject": "Verificação de Email - Atimus",
                "plainText": f"Bem-vindo à Atimus! Clique no link para verificar seu e-mail: {link_verificacao}",
                "html": f"""
                <html>
                    <body style="font-family: Arial, sans-serif; color: #333;">
                        <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px;">
                            <h1 style="color: #1e293b;">Bem-vindo à Atimus</h1>
                            <p>Obrigado por se cadastrar. Para ativar sua conta e acessar os editais, clique no botão abaixo:</p>
                            <p style="margin: 30px 0;">
                                <a href="{link_verificacao}" style="background-color: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                                    Verificar Meu E-mail
                                </a>
                            </p>
                            <p style="font-size: 12px; color: #64748b;">
                                Ou cole este link no seu navegador: <br>
                                {link_verificacao}
                            </p>
                        </div>
                    </body>
                </html>
                """
            }
        }

        poller = client.begin_send(message)
        result = poller.result()
        
        msg_id = getattr(result, 'message_id', None)
        if not msg_id and isinstance(result, dict):
            msg_id = result.get('messageId')
        
        logger.info(f"E-mail verificação enviado! MessageId: {msg_id or 'desconhecido'}")
        return True

    except Exception as e:
        logger.error(f"ERRO CRÍTICO ao enviar e-mail via ACS: {e}")
        return False


def enviar_email_recuperacao(destinatario: str, token: str) -> bool:
    """
    Envia e-mail com link para redefinição de senha.
    """
    if not AZURE_ACS_CONNECTION_STRING or not SENDER_EMAIL:
        logger.warning("ACS não configurado. E-mail de recuperação ignorado.")
        return False

    try:
        client = EmailClient.from_connection_string(AZURE_ACS_CONNECTION_STRING)
        
        # Monta link apontando para o FRONTEND, não para a API
        # Ex: https://login.atimus.agr.br/?reset_token=XYZ
        if "?" in FRONTEND_LOGIN_URL:
            link_recuperacao = f"{FRONTEND_LOGIN_URL}&reset_token={token}"
        else:
            link_recuperacao = f"{FRONTEND_LOGIN_URL}?reset_token={token}"
        
        logger.info(f"Iniciando envio de e-mail de recuperação para {destinatario}...")

        message = {
            "senderAddress": SENDER_EMAIL,
            "recipients":  {
                "to": [{"address": destinatario}]
            },
            "content": {
                "subject": "Recuperação de Senha - Atimus",
                "plainText": f"Recebemos um pedido para redefinir sua senha. Clique aqui: {link_recuperacao}",
                "html": f"""
                <html>
                    <body style="font-family: Arial, sans-serif; color: #333;">
                        <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px;">
                            <h2 style="color: #1e293b;">Redefinição de Senha</h2>
                            <p>Você solicitou a recuperação de sua senha na Atimus. Clique no botão abaixo para criar uma nova senha:</p>
                            <p style="margin: 30px 0;">
                                <a href="{link_recuperacao}" style="background-color: #ef4444; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                                    Redefinir Minha Senha
                                </a>
                            </p>
                            <p style="font-size: 14px;">Se você não solicitou isso, apenas ignore este e-mail.</p>
                            <p style="font-size: 12px; color: #64748b;">
                                Link direto: <br>
                                {link_recuperacao}
                            </p>
                        </div>
                    </body>
                </html>
                """
            }
        }

        poller = client.begin_send(message)
        result = poller.result()
        
        msg_id = getattr(result, 'message_id', None)
        if not msg_id and isinstance(result, dict):
            msg_id = result.get('messageId')
        
        logger.info(f"E-mail recuperação enviado! MessageId: {msg_id or 'desconhecido'}")
        return True

    except Exception as e:
        logger.error(f"ERRO CRÍTICO ao enviar e-mail de recuperação: {e}")
        return False