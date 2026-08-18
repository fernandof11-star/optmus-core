import requests
import sys
from datetime import datetime

WEB_URL = "https://jarvis-pessoal-nine.vercel.app"
CORE_STAGING = "https://optmus-staging.up.railway.app"
CORE_TOKEN = input("Cole seu OPTMUS_API_TOKEN: ").strip()

try:
    print(f"[{datetime.now()}] Validando staging...")
    
    # Web
    web = requests.get(f"{WEB_URL}/api/reports/monthly", timeout=15).json()
    print("✅ Web respondeu")
    
    # Core
    headers = {"Authorization": f"Bearer {CORE_TOKEN}"}
    core = requests.get(f"{CORE_STAGING}/relatorios/mensal/dados", headers=headers, timeout=15).json()
    print("✅ Core respondeu")
    
    # Comparar
    web_ind = web.get("indicadores", {})
    core_ind = core.get("indicadores", {})
    
    if len(web_ind) == len(core_ind):
        print(f"✅ {len(web_ind)} indicadores batem")
        print("🎉 STAGING APROVADO PARA PRODUÇÃO")
    else:
        print(f"❌ Web tem {len(web_ind)}, Core tem {len(core_ind)}")
        
except Exception as e:
    print(f"❌ Erro: {e}")
    sys.exit(1)
