import os
import sys
import json
import time
import html
import threading
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from axios_sync import fetch_axios_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

data_lock = threading.Lock()

def load_data():
    with data_lock:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"notes": [], "groceries": [], "backpack": "", "tasks": []}

def save_data(data):
    with data_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def load_config():
    cfg = {"bot_token": ""}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass

    # Supporto variabili d'ambiente per cloud (Koyeb, Render, Fly.io)
    if os.environ.get("BOT_TOKEN"):
        cfg["bot_token"] = os.environ.get("BOT_TOKEN")
    if os.environ.get("AXIOS_CUSTOMER_ID"):
        cfg.setdefault("axios", {})
        cfg["axios"]["customer_id"] = os.environ.get("AXIOS_CUSTOMER_ID")
        cfg["axios"]["username"] = os.environ.get("AXIOS_USERNAME", "")
        cfg["axios"]["password"] = os.environ.get("AXIOS_PASSWORD", "")

    return cfg

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def get_recycle_for_day(dt=None):
    """Calcola il rifiuto da esporre in base al calendario specificato."""
    if dt is None:
        dt = datetime.now()
    weekday = dt.weekday() # 0 = Lunedì, 6 = Domenica
    day = dt.day

    # 1° o 3° venerdì del mese: c'è anche il vetro
    is_glass_friday = (1 <= day <= 7) or (15 <= day <= 21)

    schedule = {
        0: {"type": "🌱 ORGANICO (Umido)", "details": "Esporre il mastello dell'organico"},
        1: {"type": "📦 CARTA E CARTONE", "details": "Esporre il bidoncino della carta"},
        2: {"type": "🌱 ORGANICO (Umido)", "details": "Esporre il mastello dell'organico"},
        3: {"type": "🟡 PLASTICA E METALLI", "details": "Esporre sacco giallo plastica/metalli"},
        4: {
            "type": "🌱 ORGANICO + 🍾 VETRO" if is_glass_friday else "🌱 ORGANICO (Umido)",
            "details": "⚠️ 1°/3° Venerdì: oggi esporre ANCHE il Vetro!" if is_glass_friday else "Esporre il mastello dell'organico"
        },
        5: {"type": "🗑️ INDIFFERENZIATA", "details": "Esporre il sacco secco residuo"},
        6: {"type": "⛔ NESSUNA RACCOLTA", "details": "Domenica riposo • Domani: Organico"}
    }
    return schedule.get(weekday, schedule[6])

def sync_axios_safe():
    """Scarica da Axios e aggiorna data.json in modo thread-safe."""
    result = fetch_axios_data()
    if not result:
        return False

    with data_lock:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                current_data = json.load(f)
        except Exception:
            current_data = {}

        # Preserva lo stato 'fatto' dei compiti già spuntati
        old_done_map = {t.get("text"): t.get("done", False) for t in current_data.get("tasks", [])}
        new_tasks = result.get("tasks", [])
        for t in new_tasks:
            if t.get("text") in old_done_map:
                t["done"] = old_done_map[t.get("text")]

        if new_tasks:
            current_data["tasks"] = new_tasks

        if result.get("backpack_alerts"):
            current_data["backpack"] = " • ".join(result["backpack_alerts"][-2:])

        current_data["student_name"] = result.get("student_name", "IRVINE")
        current_data["axios_last_sync"] = datetime.now().strftime("%H:%M")

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(current_data, f, indent=2, ensure_ascii=False)

    print(f"[Axios] Dati aggiornati per {current_data.get('student_name')} ({len(new_tasks)} compiti).")
    return True

# --- TELEGRAM BOT LOGIC ---
def send_telegram_message(token, chat_id, text):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"[Telegram] Errore invio: {e}")

def handle_telegram_message(token, msg):
    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from", {})
    author = from_user.get("first_name", "FAMIGLIA").upper()
    text = (msg.get("text") or "").strip()

    if not text or not chat_id:
        return

    text_lower = text.lower()
    now_time = time.strftime("%H:%M")

    # Comando /start o /aiuto
    if text_lower in ["/start", "/help", "aiuto", "/aiuto"]:
        reply = (
            "<b>🧊 Benvenuto nel Frigo di Famiglia!</b>\n\n"
            "Puoi scrivermi comandi o messaggi normali:\n\n"
            "• <code>spesa latte, biscotti, mele</code>\n"
            "  <i>(aggiunge alla spesa sul Kindle)</i>\n\n"
            "• <code>nota Comprare il pane</code>\n"
            "  <i>(attacca una nota sul frigo)</i>\n\n"
            "• <code>/cancella pane</code> o <code>/cancellanote</code>\n"
            "  <i>(elimina una nota specifica o tutte le note)</i>\n\n"
            "• <code>zaino Sacca ginnastica</code>\n"
            "  <i>(aggiorna avviso zaino scuola)</i>\n\n"
            "• <code>/compiti</code> o <code>/axios</code>\n"
            "  <i>(scarica e mostra i compiti da Axios RE!)</i>\n\n"
            "• <code>/spesa</code>\n"
            "  <i>(mostra cosa manca da comprare)</i>\n\n"
            "• <code>/fatto latte</code>\n"
            "  <i>(spunta un articolo dalla spesa)</i>"
        )
        send_telegram_message(token, chat_id, reply)
        return

    # 1. COMANDO CANCELLA NOTE DA TELEGRAM
    if text_lower in ["/cancellanote", "/puliscinote", "/svuotanote"]:
        data["notes"] = []
        save_data(data)
        send_telegram_message(token, chat_id, "🗑️ <b>Tutte le note sul frigo sono state cancellate!</b>")
        return

    if text_lower.startswith("/cancella ") or text_lower.startswith("/elimina "):
        target_str = text[10:] if text_lower.startswith("/cancella ") else text[9:]
        target_str = target_str.strip().lower()
        matched = [n for n in data.get("notes", []) if target_str in n.get("text", "").lower()]
        if matched:
            data["notes"] = [n for n in data.get("notes", []) if target_str not in n.get("text", "").lower()]
            save_data(data)
            send_telegram_message(token, chat_id, f"🗑️ <b>Eliminata nota dal frigo:</b> \"{html.escape(matched[0]['text'])}\"")
        else:
            send_telegram_message(token, chat_id, f"❓ Non ho trovato nessuna nota contenente \"{html.escape(target_str)}\".")
        return

    # 1. COMANDO AXIOS COMPITI
    if text_lower in ["/compiti", "/axios", "compiti", "cosa c'è da studiare"]:
        send_telegram_message(token, chat_id, "🔄 <i>Collegamento ad Axios RE in corso...</i>")
        sync_axios_safe()
        fresh_data = load_data()
        student = fresh_data.get("student_name", "Irvine")
        tasks = fresh_data.get("tasks", [])
        
        if not tasks:
            send_telegram_message(token, chat_id, f"📚 Nessun compito registrato per <b>{html.escape(student)}</b> su Axios.")
        else:
            lines = [f"📚 <b>Compiti per {html.escape(student)} (da Axios RE):</b>"]
            for t in tasks[:6]:
                status_icon = "✅" if t.get("done") else "▫️"
                lines.append(f"{status_icon} <b>{html.escape(t['subject'])}</b>\n   {html.escape(t['text'])}")
            
            if fresh_data.get("backpack"):
                lines.append(f"\n🎒 <b>Nota Zaino:</b> {html.escape(fresh_data['backpack'])}")

            send_telegram_message(token, chat_id, "\n\n".join(lines))
        return

    data = load_data()

    # 2. COMANDO SPESA
    if text_lower.startswith("spesa ") or text_lower.startswith("comprare "):
        items_raw = text[6:] if text_lower.startswith("spesa ") else text[9:]
        clean_items = [i.strip() for i in items_raw.replace(" e ", ",").replace("\n", ",").split(",") if i.strip()]
        
        if not clean_items:
            send_telegram_message(token, chat_id, "⚠️ Non ho capito cosa aggiungere. Es: <code>spesa pane, latte</code>")
            return

        added_names = []
        next_id = max([g.get("id", 0) for g in data.get("groceries", [])] + [0]) + 1
        for item_name in clean_items:
            data.setdefault("groceries", []).insert(0, {
                "id": next_id,
                "text": item_name.capitalize(),
                "author": author,
                "done": False
            })
            added_names.append(item_name.capitalize())
            next_id += 1
        
        save_data(data)
        safe_list = "\n• ".join([html.escape(x) for x in added_names])
        send_telegram_message(token, chat_id, f"🛒 <b>Aggiunto alla spesa sul frigo:</b>\n• {safe_list}")
        return

    # 3. COMANDO VEDI SPESA (/spesa)
    if text_lower in ["/spesa", "cosa manca", "lista spesa"]:
        pending = [g["text"] for g in data.get("groceries", []) if not g.get("done")]
        if not pending:
            send_telegram_message(token, chat_id, "🛒 <b>La lista della spesa è vuota!</b> Tutto comprato.")
        else:
            safe_list = "\n• ".join([html.escape(x) for x in pending])
            send_telegram_message(token, chat_id, f"🛒 <b>Da comprare sul frigo:</b>\n• {safe_list}")
        return

    # 4. COMANDO FATTO / SPUNTA
    if text_lower.startswith("/fatto ") or text_lower.startswith("preso "):
        query = text[7:] if text_lower.startswith("/fatto ") else text[6:]
        query = query.strip().lower()
        matched_item = None
        for g in data.get("groceries", []):
            if query in g["text"].lower() and not g.get("done"):
                g["done"] = True
                matched_item = g["text"]
                break
        if matched_item:
            save_data(data)
            send_telegram_message(token, chat_id, f"✅ Segnato come preso: <b>{html.escape(matched_item)}</b>")
        else:
            send_telegram_message(token, chat_id, f"❓ Non ho trovato \"{html.escape(query)}\" tra le cose da comprare.")
        return

    # 5. COMANDO ZAINO SCUOLA
    if text_lower.startswith("zaino "):
        note = text[6:].strip()
        data["backpack"] = note
        save_data(data)
        send_telegram_message(token, chat_id, f"🎒 <b>Aggiornato avviso zaino:</b>\n\"{html.escape(note)}\"")
        return

    # 6. NOTA FRIGO GENERALE
    note_content = text
    if text_lower.startswith("nota ") or text_lower.startswith("promemoria "):
        note_content = text.split(" ", 1)[1].strip()

    next_id = max([n.get("id", 0) for n in data.get("notes", [])] + [0]) + 1
    new_note = {
        "id": next_id,
        "author": author,
        "text": note_content,
        "time": now_time
    }
    data.setdefault("notes", []).insert(0, new_note)
    data["notes"] = data["notes"][:8]
    save_data(data)

    send_telegram_message(token, chat_id, f"📌 <b>Nota pubblicata sul Kindle ({html.escape(author)}):</b>\n\"{html.escape(note_content)}\"")

def telegram_poller_thread():
    last_offset = 0
    print("[Telegram] Poller avviato.")
    while True:
        cfg = load_config()
        token = cfg.get("bot_token", "").strip()
        if not token:
            time.sleep(3)
            continue

        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={last_offset}&timeout=20"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KindleFridgeBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    for update in res.get("result", []):
                        last_offset = update["update_id"] + 1
                        msg = update.get("message")
                        if msg:
                            handle_telegram_message(token, msg)
        except Exception:
            time.sleep(4)

def periodic_axios_sync():
    """Sincronizza Axios all'avvio e poi ogni 2 ore."""
    time.sleep(2)
    sync_axios_safe()
    while True:
        time.sleep(2 * 3600)
        try:
            sync_axios_safe()
        except Exception as e:
            print("[Axios Scheduler] Errore:", e)

# --- HTTP SERVER CON API REST ---
class FridgeRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/api/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            data = load_data()
            cfg = load_config()
            data["bot_active"] = bool(cfg.get("bot_token", "").strip())
            data["recycle"] = get_recycle_for_day()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(INDEX_FILE, "rb") as f:
                self.wfile.write(f.read())
            return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        
        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception:
            req_data = {}

        data = load_data()

        if parsed.path == "/api/delete-note":
            nid = req_data.get("id")
            data["notes"] = [n for n in data.get("notes", []) if n.get("id") != nid]
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/clear-notes":
            data["notes"] = []
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/toggle-grocery":
            gid = req_data.get("id")
            for item in data.get("groceries", []):
                if item.get("id") == gid:
                    item["done"] = not item.get("done", False)
                    break
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/toggle-task":
            tid = req_data.get("id")
            for item in data.get("tasks", []):
                if item.get("id") == tid:
                    item["done"] = not item.get("done", False)
                    break
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/reset-groceries":
            for item in data.get("groceries", []):
                item["done"] = False
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/clear-groceries":
            data["groceries"] = [g for g in data.get("groceries", []) if not g.get("done")]
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/sync-axios":
            sync_axios_safe()
            self._send_json({"ok": True, "data": load_data()})
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

def run_server(port=8000):
    os.chdir(BASE_DIR)
    
    # Thread Telegram
    t1 = threading.Thread(target=telegram_poller_thread, daemon=True)
    t1.start()

    # Thread Scheduler Axios
    t2 = threading.Thread(target=periodic_axios_sync, daemon=True)
    t2.start()

    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, FridgeRequestHandler)
    print(f"[Server] Dashboard attiva su http://localhost:{port}")
    httpd.serve_forever()

if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
    elif len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = 8000
    run_server(port)
