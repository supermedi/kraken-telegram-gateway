from sqlmodel import Session, create_engine
from kraken_telegram_gateway.gateway.service import get_scalp_session_detail, get_scalp_audit

print("Import réussi !")
try:
    print(f"get_scalp_session_detail: {get_scalp_session_detail}")
    print(f"get_scalp_audit: {get_scalp_audit}")
    print("Test d'importation OK.")
except Exception as e:
    print(f"Erreur lors de l'accès aux fonctions: {e}")
