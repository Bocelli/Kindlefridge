# 🧊 Kindle Fridge Dashboard

Dashboard interattiva per Kindle (ottimizzata per E-Ink 7") con:
- 💬 **Bacheca Famiglia & Spesa via Bot Telegram**
- 🎒 **Integrazione Registro Elettronico Axios (compiti e avvisi zaino)**
- ☀️ **Meteo locale & Raccolta Differenziata**
- ⚡ **Design Swiss Minimalist ad altissimo contrasto (zero emoji, 100% SVG)**

---

## 🚀 Avvio Rapido Locale

1. Clona il repository:
   ```bash
   git clone https://github.com/Bocelli/Kindlefridge.git
   cd Kindlefridge
   ```

2. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```

3. Copia il file di configurazione:
   ```bash
   cp config.example.json config.json
   ```
   *(Modifica `config.json` con il tuo Token Telegram e i dati Axios).*

4. Avvia il server:
   ```bash
   python server.py
   ```
   Apri nel browser o sul Kindle: `http://localhost:8000` (oppure `http://IP_LOCALE:8000`).

---

## ☁️ Deploy su Cloud Gratuito (Koyeb / Render / Fly.io)

Il progetto supporta le variabili d'ambiente senza bisogno di esporre `config.json`:
- `BOT_TOKEN`: Il token del bot Telegram
- `AXIOS_CUSTOMER_ID`: Codice Fiscale della Scuola
- `AXIOS_USERNAME`: Codice Utente / Username
- `AXIOS_PASSWORD`: Password Registro Axios
- `PORT`: Porta HTTP (impostata automaticamente dai cloud)

---

## 📱 Comandi Telegram

- `spesa latte, uova, pane` -> Aggiunge articoli alla lista della spesa
- `nota Ricordarsi il pane` -> Pubblica un post-it sul Kindle
- `zaino Sacca ginnastica` -> Aggiorna l'avviso dello zaino
- `/compiti` o `/axios` -> Scarica e visualizza i compiti aggiornati da Axios
- `/spesa` -> Mostra la lista delle cose da comprare
- `/fatto latte` -> Spunta l'articolo come preso
