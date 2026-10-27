import os
import re
import json
import requests
from lxml import html

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DATA_FILE = os.path.join(BASE_DIR, "data.json")

def load_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass

    # Supporto variabili d'ambiente per cloud
    if os.environ.get("AXIOS_CUSTOMER_ID"):
        cfg.setdefault("axios", {})
        cfg["axios"]["customer_id"] = os.environ.get("AXIOS_CUSTOMER_ID")
        cfg["axios"]["username"] = os.environ.get("AXIOS_USERNAME", "")
        cfg["axios"]["password"] = os.environ.get("AXIOS_PASSWORD", "")

    return cfg

def fetch_axios_data():
    """Effettua il login su Axios Scuola Digitale e preleva i compiti e gli avvisi."""
    cfg = load_config().get("axios", {})
    customer_id = cfg.get("customer_id", "").strip()
    username = cfg.get("username", "").strip()
    password = cfg.get("password", "").strip()

    if not customer_id or not username or not password:
        print("[Axios] Credenziali incomplete in config.json")
        return None

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://registrofamiglie.axioscloud.it",
        "Referer": f"https://registrofamiglie.axioscloud.it/Pages/SD/SD_Login.aspx?Customer_id={customer_id}",
        "X-Requested-With": "XMLHttpRequest"
    })

    try:
        # 1. Inizializza sessione
        s.get(f"https://registrofamiglie.axioscloud.it/Pages/SD/SD_Login.aspx?Customer_id={customer_id}", timeout=15)

        # 2. Login
        login_res = s.post("https://registrofamiglie.axioscloud.it/Pages/SD/SD_Login.aspx", data={
            "customerid": customer_id,
            "username": username,
            "password": password
        }, timeout=15)

        if "SD_Dashboard.aspx" not in login_res.url:
            print("[Axios] Login non riuscito:", login_res.url)
            return None

        # 3. Carica Dashboard (indispensabile per impostare l'alunno attivo)
        dash_res = s.post("https://registrofamiglie.axioscloud.it/Pages/APP/APP_Ajax_Get.aspx?action=DASHBOARDLOAD", timeout=15)
        
        # Estrai nome alunno
        student_name = "ALUNNO"
        tree_dash = html.fromstring(dash_res.text)
        alunno_el = tree_dash.xpath("//*[@data-action='FAMILY_CHANGE_ALUNNO']")
        if alunno_el:
            student_name = alunno_el[0].text_content().strip()

        # 4. Richiedi Registro di classe
        reg_res = s.post("https://registrofamiglie.axioscloud.it/Pages/APP/APP_Ajax_Get.aspx?action=FAMILY_REGISTRO_CLASSE", timeout=20)
        j_reg = reg_res.json()

        if j_reg.get("errorcode") != "0" or "html" not in j_reg:
            print("[Axios] Errore risposta registro:", j_reg.get("errormsg"))
            return None

        tree = html.fromstring(j_reg["html"])
        rows = tree.xpath("//table//tr")

        homework_list = []
        backpack_alerts = []
        task_id = 1

        for row in rows:
            cells = [c.text_content().strip().replace('\r', '').replace('\n', ' ') for c in row.xpath("./th | ./td")]
            if len(cells) < 7 or cells[0].lower().startswith("data"):
                continue

            date_raw = cells[0] # es: "27/10/2026martedì"
            compiti = cells[2]
            annotazioni = cells[5]

            # Estrai data numerica pulita
            m_date = re.search(r'(\d{2}/\d{2}/\d{4})', date_raw)
            clean_date = m_date.group(1) if m_date else date_raw

            # Se ci sono compiti
            if compiti:
                parts = re.split(r'([A-ZÀ-Ú\s]{3,20}:)', compiti)
                if len(parts) > 1:
                    i = 1
                    while i < len(parts):
                        subj = parts[i].replace(":", "").strip()
                        text = parts[i+1].strip() if i+1 < len(parts) else ""
                        if text:
                            homework_list.append({
                                "id": task_id,
                                "date": clean_date,
                                "subject": f"{subj} • {clean_date}",
                                "text": text,
                                "done": False
                            })
                            task_id += 1
                        i += 2
                else:
                    homework_list.append({
                        "id": task_id,
                        "date": clean_date,
                        "subject": f"Compiti per {clean_date}",
                        "text": compiti,
                        "done": False
                    })
                    task_id += 1

            # Annotazioni del giorno per zaino
            if annotazioni:
                backpack_alerts.append(f"{clean_date}: {annotazioni}")

        return {
            "student_name": student_name,
            "tasks": homework_list,
            "backpack_alerts": backpack_alerts
        }

    except Exception as e:
        print("[Axios] Eccezione sync:", e)
        return None

if __name__ == "__main__":
    res = fetch_axios_data()
    print("Test fetch result:", res.get("student_name") if res else "None")
