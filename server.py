import os
import sys
import re
import json
import time
import html
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from axios_sync import fetch_axios_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

data_lock = threading.Lock()

def get_rome_now():
    """Restituisce data e ora attuali nel fuso orario di Roma (CET UTC+1 / CEST UTC+2)."""
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo("Europe/Rome"))
    except Exception:
        pass

    # Calcolo matematico deterministico per fuso orario italiano
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year
    march_last = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    dst_start = march_last - timedelta(days=(march_last.weekday() + 1) % 7)
    oct_last = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    dst_end = oct_last - timedelta(days=(oct_last.weekday() + 1) % 7)

    tz = timezone(timedelta(hours=2)) if dst_start <= now_utc < dst_end else timezone(timedelta(hours=1))
    return now_utc.astimezone(tz)

def load_data():
    with data_lock:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("events", [])
                d.setdefault("notes", [])
                d.setdefault("groceries", [])
                d.setdefault("tasks", [])
                return d
        except Exception:
            return {"notes": [], "groceries": [], "backpack": "", "tasks": [], "events": []}

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
        dt = get_rome_now()
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
        current_data["axios_last_sync"] = get_rome_now().strftime("%H:%M")

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
    now_time = get_rome_now().strftime("%H:%M")

    # Comando /start o /aiuto
    if text_lower in ["/start", "/help", "aiuto", "/aiuto"]:
        reply = (
            "<b>🧊 Benvenuto nel Frigo di Famiglia!</b>\n\n"
            "Puoi inviarmi comandi o messaggi normali:\n\n"
            "🕒 <b>IMPEGNI DI POMERIGGIO:</b>\n"
            "• <code>impegno 17:30 Dentista</code>\n"
            "• <code>impegno 18:00 Basket Palazzetto</code>\n"
            "• <code>/impegni</code> <i>(mostra impegni di oggi)</i>\n"
            "• <code>/fatto basket</code> <i>(spunta come completato)</i>\n"
            "• <code>/cancellaimpegno dentista</code>\n\n"
            "🛒 <b>SPESA:</b>\n"
            "• <code>spesa latte, biscotti, mele</code>\n"
            "• <code>/spesa</code> <i>(mostra cosa manca)</i>\n"
            "• <code>/fatto latte</code> <i>(spunta come comprato)</i>\n\n"
            "📌 <b>NOTE SUL FRIGO:</b>\n"
            "• <code>nota Ricordati di firmare l'uscita</code>\n"
            "• <code>/cancella firmare</code> o <code>/cancellanote</code>\n\n"
            "📚 <b>SCUOLA AXIOS:</b>\n"
            "• <code>/compiti</code> <i>(scarica compiti dal RE)</i>\n"
            "• <code>zaino Portare scarpe da ginnastica</code>"
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

    # 2. GESTIONE IMPEGNI POMERIGGIO
    if text_lower in ["/cancellaimpegni", "/pulisciimpegni", "/svuotaimpegni"]:
        data["events"] = []
        save_data(data)
        send_telegram_message(token, chat_id, "🗑️ <b>Tutti gli impegni sul frigo sono stati cancellati!</b>")
        return

    if text_lower.startswith("/cancellaimpegno ") or text_lower.startswith("/eliminaimpegno "):
        target_str = text.split(" ", 1)[1].strip().lower()
        matched = [e for e in data.get("events", []) if target_str in e.get("text", "").lower()]
        if matched:
            data["events"] = [e for e in data.get("events", []) if target_str not in e.get("text", "").lower()]
            save_data(data)
            send_telegram_message(token, chat_id, f"🗑️ <b>Eliminato impegno dal frigo:</b> \"{html.escape(matched[0]['text'])}\"")
        else:
            send_telegram_message(token, chat_id, f"❓ Non ho trovato nessun impegno contenente \"{html.escape(target_str)}\".")
        return

    if text_lower in ["/impegni", "impegni", "appuntamenti", "cosa c'è oggi"]:
        events = data.get("events", [])
        if not events:
            send_telegram_message(token, chat_id, "🕒 <b>Nessun impegno in programma per oggi sul Kindle!</b>\nPuoi aggiungerne uno con: <code>impegno 17:30 Dentista</code>")
        else:
            lines = ["🕒 <b>Impegni di Pomeriggio:</b>"]
            for ev in events:
                st = "✅" if ev.get("done") else "▫️"
                t_str = f"[{ev.get('time')}] " if ev.get('time') and ev.get('time') != "--:--" else ""
                lines.append(f"{st} {t_str}<b>{html.escape(ev.get('text', ''))}</b>")
            send_telegram_message(token, chat_id, "\n".join(lines))
        return

    if text_lower.startswith("impegno ") or text_lower.startswith("appuntamento ") or text_lower.startswith("evento "):
        raw_content = text.split(" ", 1)[1].strip()
        m_time = re.match(r'^(\d{1,2}[:.]\d{2}(?:\s*-\s*\d{1,2}[:.]\d{2})?)\s+(.*)', raw_content)
        if m_time:
            event_time = m_time.group(1).replace(".", ":")
            event_text = m_time.group(2).strip()
        else:
            event_time = "--:--"
            event_text = raw_content

        next_id = max([e.get("id", 0) for e in data.get("events", [])] + [0]) + 1
        data.setdefault("events", []).append({
            "id": next_id,
            "time": event_time,
            "text": event_text,
            "done": False
        })
        save_data(data)
        time_label = f"[{event_time}] " if event_time != "--:--" else ""
        send_telegram_message(token, chat_id, f"🕒 <b>Impegno aggiunto sul Kindle:</b>\n{time_label}<b>{html.escape(event_text)}</b>")
        return

    # 3. COMANDO AXIOS COMPITI
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

    # 4. COMANDO SPESA
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

    # 5. COMANDO VEDI SPESA (/spesa)
    if text_lower in ["/spesa", "cosa manca", "lista spesa"]:
        pending = [g["text"] for g in data.get("groceries", []) if not g.get("done")]
        if not pending:
            send_telegram_message(token, chat_id, "🛒 <b>La lista della spesa è vuota!</b> Tutto comprato.")
        else:
            safe_list = "\n• ".join([html.escape(x) for x in pending])
            send_telegram_message(token, chat_id, f"🛒 <b>Da comprare sul frigo:</b>\n• {safe_list}")
        return

    # 6. COMANDO FATTO / SPUNTA (IMPEGNO O SPESA)
    if text_lower.startswith("/fatto ") or text_lower.startswith("preso ") or text_lower.startswith("/fattoimpegno "):
        query = text.split(" ", 1)[1].strip().lower()

        # Prima verifica se corrisponde a un impegno
        matched_event = None
        for ev in data.get("events", []):
            if query in ev.get("text", "").lower() and not ev.get("done"):
                ev["done"] = True
                matched_event = ev.get("text")
                break
        if matched_event:
            save_data(data)
            send_telegram_message(token, chat_id, f"✅ Impegno completato: <b>{html.escape(matched_event)}</b>")
            return

        # Poi verifica se corrisponde alla spesa
        matched_item = None
        for g in data.get("groceries", []):
            if query in g["text"].lower() and not g.get("done"):
                g["done"] = True
                matched_item = g["text"]
                break
        if matched_item:
            save_data(data)
            send_telegram_message(token, chat_id, f"✅ Segnato come preso: <b>{html.escape(matched_item)}</b>")
            return

        send_telegram_message(token, chat_id, f"❓ Non ho trovato \"{html.escape(query)}\" né tra gli impegni né nella spesa.")
        return

    # 7. COMANDO ZAINO SCUOLA
    if text_lower.startswith("zaino "):
        note = text[6:].strip()
        data["backpack"] = note
        save_data(data)
        send_telegram_message(token, chat_id, f"🎒 <b>Aggiornato avviso zaino:</b>\n\"{html.escape(note)}\"")
        return

    # 8. NOTA FRIGO GENERALE
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
            rn = get_rome_now()
            data["bot_active"] = bool(cfg.get("bot_token", "").strip())
            data["recycle"] = get_recycle_for_day(rn)
            data["server_time"] = {
                "hours": rn.hour,
                "minutes": rn.minute,
                "seconds": rn.second,
                "timestamp_ms": int(rn.timestamp() * 1000)
            }
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

        if parsed.path == "/api/toggle-event":
            eid = req_data.get("id")
            for item in data.get("events", []):
                if item.get("id") == eid:
                    item["done"] = not item.get("done", False)
                    break
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/delete-event":
            eid = req_data.get("id")
            data["events"] = [e for e in data.get("events", []) if e.get("id") != eid]
            save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/add-event":
            time_val = (req_data.get("time") or "--:--").strip()
            text_val = (req_data.get("text") or "").strip()
            if text_val:
                next_id = max([e.get("id", 0) for e in data.get("events", [])] + [0]) + 1
                data.setdefault("events", []).append({
                    "id": next_id,
                    "time": time_val,
                    "text": text_val,
                    "done": False
                })
                save_data(data)
            self._send_json({"ok": True, "data": data})
            return

        if parsed.path == "/api/clear-events":
            data["events"] = [e for e in data.get("events", []) if not e.get("done")]
            save_data(data)
            self._send_json({"ok": True, "data": data})
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
