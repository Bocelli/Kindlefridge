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
                d.setdefault("notes", [])
                d.setdefault("groceries", [])
                d.setdefault("tasks", [])
                return d
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
    """Calcola il rifiuto da esporre stasera per il passaggio del giorno successivo."""
    if dt is None:
        dt = get_rome_now()

    # Si espone la sera prima del passaggio mattutino del camion
    target_dt = dt + timedelta(days=1)
    weekday = target_dt.weekday() # 0 = Lunedì, 6 = Domenica
    day = target_dt.day

    DAYS_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    target_day_name = DAYS_IT[weekday]

    # 1° o 3° venerdì del mese: c'è anche il vetro
    is_glass_friday = (1 <= day <= 7) or (15 <= day <= 21)

    schedule = {
        0: {
            "type": "ORGANICO (Umido)",
            "details": f"Ritiro domani ({target_day_name}) • Esporre il mastello stasera"
        },
        1: {
            "type": "CARTA E CARTONE",
            "details": f"Ritiro domani ({target_day_name}) • Esporre il bidoncino stasera"
        },
        2: {
            "type": "ORGANICO (Umido)",
            "details": f"Ritiro domani ({target_day_name}) • Esporre il mastello stasera"
        },
        3: {
            "type": "PLASTICA E METALLI",
            "details": f"Ritiro domani ({target_day_name}) • Esporre sacco giallo stasera"
        },
        4: {
            "type": "ORGANICO + VETRO" if is_glass_friday else "ORGANICO (Umido)",
            "details": f"Attenzione: domani ({target_day_name}) anche Vetro!" if is_glass_friday else f"Ritiro domani ({target_day_name}) • Esporre il mastello stasera"
        },
        5: {
            "type": "INDIFFERENZIATA",
            "details": f"Ritiro domani ({target_day_name}) • Esporre sacco secco residuo stasera"
        },
        6: {
            "type": "NESSUNA RACCOLTA",
            "details": f"Domenica riposo (nessun ritiro) • Domani sera: Organico"
        }
    }
    res = schedule.get(weekday, schedule[6]).copy()
    res["target_day"] = target_day_name
    return res

# --- METEO SALEMI (TP) ---
WMO_CODES = {
    0: 'SERENO', 1: 'QUASI SERENO', 2: 'POCO NUVOLOSO', 3: 'COPERTO',
    45: 'NEBBIA', 48: 'NEBBIA',
    51: 'PIOVIGGINE', 53: 'PIOVIGGINE', 55: 'PIOVIGGINE',
    61: 'PIOGGIA DEBOLE', 63: 'PIOGGIA', 65: 'FORTE PIOGGIA',
    71: 'NEVE', 73: 'NEVE', 75: 'FORTE NEVE',
    80: 'ROVESCI', 81: 'ROVESCI', 82: 'FORTI ROVESCI',
    95: 'TEMPORALE', 96: 'TEMPORALE', 99: 'TEMPORALE'
}

cached_weather = None
last_weather_time = 0

def get_salemi_weather():
    global cached_weather, last_weather_time
    now_ts = time.time()
    # Cache per 20 minuti
    if cached_weather and (now_ts - last_weather_time < 1200):
        return cached_weather

    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=37.818&longitude=12.801&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code&hourly=temperature_2m,precipitation_probability,weather_code&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Europe%2FRome"
        req = urllib.request.Request(url, headers={"User-Agent": "KindleFridge/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        curr = res.get("current", {})
        daily = res.get("daily", {})
        hourly = res.get("hourly", {})

        curr_temp = round(curr.get("temperature_2m", 0))
        curr_code = curr.get("weather_code", 0)
        curr_desc = WMO_CODES.get(curr_code, "VARIABILE")
        min_temp = round(daily.get("temperature_2m_min", [0])[0])
        max_temp = round(daily.get("temperature_2m_max", [0])[0])
        rain_prob = daily.get("precipitation_probability_max", [0])[0]

        curr_iso_prefix = curr.get("time", "")[:13]
        h_times = hourly.get("time", [])
        curr_idx = 0
        for idx, ht in enumerate(h_times):
            if ht.startswith(curr_iso_prefix):
                curr_idx = idx
                break

        forecast_slots = []
        for step in [6, 12]:
            target_idx = curr_idx + step
            if target_idx < len(h_times):
                t_str = h_times[target_idx].split("T")[1]
                temp_val = round(hourly["temperature_2m"][target_idx])
                c_val = hourly["weather_code"][target_idx]
                desc_val = WMO_CODES.get(c_val, "Variabile").capitalize()
                forecast_slots.append({"time": t_str, "temp": f"{temp_val}°", "desc": desc_val, "code": c_val})

        cached_weather = {
            "city": "Salemi (TP)",
            "temp": f"{curr_temp}°C",
            "condition": curr_desc,
            "min_max": f"MIN {min_temp}° • MAX {max_temp}°",
            "rain": f"PIOGGIA {rain_prob}%",
            "code": curr_code,
            "forecast": forecast_slots
        }
        last_weather_time = now_ts
        return cached_weather
    except Exception as e:
        print("[Meteo] Errore fetch Open-Meteo:", e)
        if cached_weather:
            return cached_weather
        return {
            "city": "Salemi (TP)",
            "temp": "15°C",
            "condition": "POCO NUVOLOSO",
            "min_max": "MIN 12° • MAX 21°",
            "rain": "PIOGGIA 0%",
            "code": 2,
            "forecast": [
                {"time": "03:00", "temp": "13°", "desc": "Poco nuv.", "code": 2},
                {"time": "09:00", "temp": "18°", "desc": "Sereno", "code": 0}
            ]
        }

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
def setup_telegram_commands(token):
    """Registra la lista comandi ufficiale nell'interfaccia di Telegram (menu [/])."""
    if not token:
        return
    commands = [
        {"command": "comandi", "description": "📖 Riepilogo completo di tutti i comandi"},
        {"command": "spesa", "description": "🛒 Mostra la lista della spesa"},
        {"command": "compiti", "description": "📚 Scarica compiti dal Registro Axios RE"},
        {"command": "meteo", "description": "🌤️ Previsioni meteo in tempo reale Salemi"},
        {"command": "differenziata", "description": "♻️ Rifiuto da esporre stasera/domani"},
        {"command": "cancellanote", "description": "🗑️ Cancella tutte le note sul frigo"}
    ]
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    payload = json.dumps({"commands": commands}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("[Telegram] Comandi registrati su Telegram con setMyCommands.")
    except Exception as e:
        print(f"[Telegram] Errore setMyCommands: {e}")

def get_commands_recap_message():
    return (
        "<b>📋 RIEPILOGO COMANDI FRIGO KINDLE</b>\n\n"
        "Ecco tutto quello che puoi inviarmi direttamente qui in chat:\n\n"
        "🛒 <b>1. SPESA DI FAMIGLIA</b>\n"
        "• <code>spesa latte, pasta, mele</code> <i>(aggiunge articoli)</i>\n"
        "• <code>/spesa</code> <i>(mostra cosa c'è da comprare)</i>\n"
        "• <code>/fatto latte</code> <i>(spunta come comprato)</i>\n\n"
        "📌 <b>2. NOTE SULLA BACHECA</b>\n"
        "• <code>nota Ricordati di firmare l'uscita</code>\n"
        "• <i>(oppure scrivi qualsiasi messaggio normale)</i>\n"
        "• <code>/cancella firmare</code> <i>(elimina specifica nota)</i>\n"
        "• <code>/cancellanote</code> <i>(svuota tutte le note)</i>\n\n"
        "📚 <b>3. SCUOLA & AXIOS RE</b>\n"
        "• <code>/compiti</code> <i>(scarica compiti aggiornati dal RE)</i>\n"
        "• <code>zaino Sacca scarpe ginnastica</code> <i>(avviso zaino)</i>\n\n"
        "🌤️ <b>4. METEO & DIFFERENZIATA</b>\n"
        "• <code>/meteo</code> <i>(meteo in tempo reale a Salemi TP)</i>\n"
        "• <code>/differenziata</code> <i>(cosa esporre stasera)</i>\n\n"
        "<i>💡 Suggerimento: tocca l'icona <b>[/]</b> in basso a sinistra nella tastiera per aprire il menu rapido dei comandi!</i>"
    )

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

    # 0. RECAP DEI COMANDI
    if text_lower in ["/comandi", "comandi", "/help", "help", "/start", "start", "/aiuto", "aiuto", "/menu", "menu", "istruzioni", "cosa puoi fare"]:
        send_telegram_message(token, chat_id, get_commands_recap_message())
        return

    # COMANDO DIFFERENZIATA
    if text_lower in ["/differenziata", "differenziata", "/rifiuti", "rifiuti", "spazzatura", "cosa si butta", "bidoni"]:
        rec = get_recycle_for_day()
        target_day = rec.get("target_day", "domani")
        reply_diff = (
            f"♻️ <b>Raccolta Differenziata (Salemi):</b>\n\n"
            f"• <b>Stasera da esporre (per {target_day}):</b>\n  {rec['type']}\n\n"
            f"• <b>Istruzioni per il ritiro:</b>\n  {rec['details']}"
        )
        send_telegram_message(token, chat_id, reply_diff)
        return

    # COMANDO METEO SALEMI (TP)
    if text_lower in ["/meteo", "meteo", "che tempo fa", "previsioni"]:
        w = get_salemi_weather()
        msg_meteo = (
            f"🌤️ <b>Meteo a Salemi (TP):</b>\n\n"
            f"• <b>Temperatura:</b> {w['temp']} ({w['condition'].capitalize()})\n"
            f"• <b>Escursione:</b> {w['min_max']}\n"
            f"• <b>Probabilità pioggia:</b> {w['rain'].replace('PIOGGIA ', '')}\n\n"
            f"<b>Prossime ore:</b>\n"
        )
        for s in w.get("forecast", []):
            msg_meteo += f"• ore {s['time']}: {s['temp']} ({s['desc']})\n"
        send_telegram_message(token, chat_id, msg_meteo)
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

    # 2. COMANDO AXIOS COMPITI
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
            for t in tasks[:8]:
                status_icon = "✅" if t.get("done") else "▫️"
                date_val = t.get("date", "")
                m_d = re.search(r"(\d{1,2}/\d{1,2})", date_val)
                date_label = f" (per il {m_d.group(1)})" if m_d else ""
                lines.append(f"{status_icon} <b>{html.escape(t['subject'])}{date_label}:</b>\n   {html.escape(t['text'])}")
            
            if fresh_data.get("backpack"):
                lines.append(f"\n🎒 <b>Nota Zaino:</b> {html.escape(fresh_data['backpack'])}")

            send_telegram_message(token, chat_id, "\n\n".join(lines))
        return

    data = load_data()

    # 3. COMANDO SPESA
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

    # 4. COMANDO VEDI SPESA (/spesa)
    if text_lower in ["/spesa", "cosa manca", "lista spesa"]:
        pending = [g["text"] for g in data.get("groceries", []) if not g.get("done")]
        if not pending:
            send_telegram_message(token, chat_id, "🛒 <b>La lista della spesa è vuota!</b> Tutto comprato.")
        else:
            safe_list = "\n• ".join([html.escape(x) for x in pending])
            send_telegram_message(token, chat_id, f"🛒 <b>Da comprare sul frigo:</b>\n• {safe_list}")
        return

    # 5. COMANDO FATTO / SPUNTA (SPESA)
    if text_lower.startswith("/fatto ") or text_lower.startswith("preso "):
        query = text.split(" ", 1)[1].strip().lower()

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

        send_telegram_message(token, chat_id, f"❓ Non ho trovato \"{html.escape(query)}\" tra le cose da comprare.")
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
    commands_registered_token = None
    print("[Telegram] Poller avviato.")
    while True:
        cfg = load_config()
        token = cfg.get("bot_token", "").strip()
        if not token:
            time.sleep(3)
            continue

        if token != commands_registered_token:
            setup_telegram_commands(token)
            commands_registered_token = token

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
            data["weather"] = get_salemi_weather()
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


        if parsed.path == "/api/set-token":
            tok = req_data.get("token", "").strip()
            cfg = load_config()
            cfg["bot_token"] = tok
            save_config(cfg)
            if tok:
                setup_telegram_commands(tok)
            self._send_json({"ok": True})
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
