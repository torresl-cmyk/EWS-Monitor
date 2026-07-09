#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EWS Alert - Monitor del "Apocalypse Early Warning System" (ews.kylemcdonald.net)

Consulta el nivel de emergencia y avisa por Telegram cuando supera tu umbral.
Sin dependencias externas: solo biblioteca estandar de Python 3.

Dos modos (variable de entorno EWS_MODE):
  - "rss"        : usa /rss.xml. El feed SOLO trae items cuando el sitio llega a
                   nivel 5, asi que en este modo solo se avisa en nivel 5.
                   No requiere ninguna URL adicional. Util como red de seguridad.
  - "dashboard"  : usa el dashboard.json (R2) para leer el nivel 1-5 en vivo y
                   aplicar TU umbral (EWS_THRESHOLD). Requiere EWS_DASHBOARD_URL.

Como obtener EWS_DASHBOARD_URL:
  Abri https://ews.kylemcdonald.net/ -> F12 -> pestania Network -> recarga ->
  filtra por "json" o "dashboard". La URL del request (Cloudflare R2) es esa.

Variables de entorno:
  EWS_MODE            "rss" (default) o "dashboard"
  EWS_RSS_URL         default "https://ews.kylemcdonald.net/rss.xml"
  EWS_DASHBOARD_URL   URL del dashboard.json (solo modo dashboard)
  EWS_THRESHOLD       nivel minimo para avisar en modo dashboard (default 4)
  TELEGRAM_BOT_TOKEN  token del bot (de @BotFather)
  TELEGRAM_CHAT_ID    tu chat id (de @userinfobot)
  EWS_STATE_FILE      default "ews_state.json"  (anti-spam / dedup)
  EWS_LOG_FILE        default "ews_log.csv"     (archivo historico propio)

Uso:
  python ews_alert.py            # consulta y, si corresponde, avisa
  python ews_alert.py --dry-run  # no manda nada a Telegram, solo imprime
"""

import os
import sys
import json
import csv
import datetime
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

# Carpeta del propio script: anclamos los archivos de datos aca, asi no
# dependemos del directorio desde el que se ejecute.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------- Configuracion -----------------------------
MODE          = (os.getenv("EWS_MODE") or "rss").strip().lower()
RSS_URL       = os.getenv("EWS_RSS_URL", "https://ews.kylemcdonald.net/rss.xml")
DASHBOARD_URL = os.getenv("EWS_DASHBOARD_URL", "").strip()
THRESHOLD     = int(os.getenv("EWS_THRESHOLD") or "4")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE    = os.getenv("EWS_STATE_FILE", os.path.join(BASE_DIR, "ews_state.json"))
LOG_FILE      = os.getenv("EWS_LOG_FILE", os.path.join(BASE_DIR, "ews_log.csv"))
HEARTBEAT     = (os.getenv("EWS_HEARTBEAT") or "").strip() not in ("", "0")

TIMEOUT = 25
USER_AGENT = "ews-alert-personal/1.0 (uso personal; cadencia 30min)"
DRY_RUN = "--dry-run" in sys.argv


# ------------------------------- Utilidades -------------------------------
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def http_get(url):
    """GET simple con timeout y user-agent. Devuelve bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    """Persiste el estado. No es critico: si falla, avisa y sigue."""
    try:
        _ensure_parent(STATE_FILE)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("AVISO: no pude guardar el estado ({}): {}".format(STATE_FILE, e),
              file=sys.stderr)


def log_row(row):
    """Append al CSV historico. No es critico: si falla, avisa y sigue."""
    try:
        _ensure_parent(LOG_FILE)
        new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp_utc", "mode", "level", "airborne",
                            "expected", "alerted", "detail"])
            w.writerow(row)
    except OSError as e:
        print("AVISO: no pude escribir el log ({}): {}".format(LOG_FILE, e),
              file=sys.stderr)


def send_telegram(text):
    """Manda un mensaje a tu chat. Devuelve True si salio bien."""
    if DRY_RUN:
        print("[DRY-RUN] Telegram:\n" + text)
        return True
    if not TG_TOKEN or not TG_CHAT:
        print("ERROR: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.", file=sys.stderr)
        return False
    url = "https://api.telegram.org/bot{}/sendMessage".format(TG_TOKEN)
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT}),
            timeout=TIMEOUT,
        ) as resp:
            ok = json.loads(resp.read().decode("utf-8")).get("ok", False)
            if not ok:
                print("ERROR: Telegram respondio sin ok.", file=sys.stderr)
            return ok
    except urllib.error.URLError as e:
        print("ERROR enviando Telegram:", e, file=sys.stderr)
        return False


# -------------------------- Lectura del dashboard --------------------------
def _find_first(d, candidates):
    """Busca la primera clave existente (recursivo simple) entre 'candidates'."""
    if isinstance(d, dict):
        for k in candidates:
            if k in d and d[k] is not None:
                return d[k]
        for v in d.values():
            r = _find_first(v, candidates)
            if r is not None:
                return r
    return None


def check_dashboard():
    """Modo dashboard: lee nivel 1-5 en vivo y aplica EWS_THRESHOLD."""
    if not DASHBOARD_URL:
        print("ERROR: EWS_DASHBOARD_URL vacio. Conseguilo desde DevTools > Network.",
              file=sys.stderr)
        sys.exit(2)

    raw = http_get(DASHBOARD_URL)
    data = json.loads(raw.decode("utf-8"))

    # Los nombres de campo pueden variar: probamos los mas probables.
    level    = _find_first(data, ["level", "emergency_level", "emergencyLevel", "alert_level"])
    airborne = _find_first(data, ["airborne", "airborne_count", "current", "count"])
    expected = _find_first(data, ["expected", "baseline", "expected_count", "mean"])

    if level is None:
        # Si no encuentra el nivel, te muestra el JSON para que ajustes claves.
        print("No pude identificar el nivel. Claves de primer nivel del JSON:",
              file=sys.stderr)
        print(list(data.keys()) if isinstance(data, dict) else type(data),
              file=sys.stderr)
        sys.exit(3)

    level = int(level)
    state = load_state()
    last_level = state.get("last_alert_level", 0)

    alerted = False
    # Avisa al cruzar hacia arriba el umbral (histeresis: no repite mismo nivel).
    if level >= THRESHOLD and level > last_level:
        extra = ""
        if airborne is not None and expected is not None:
            try:
                diff = int(airborne) - int(expected)
                extra = "\n{} en el aire ({:+d} sobre lo esperado)".format(int(airborne), diff)
            except (TypeError, ValueError):
                pass
        msg = ("EWS - nivel de emergencia {} (umbral {})"
               "{}\nhttps://ews.kylemcdonald.net/").format(level, THRESHOLD, extra)
        alerted = send_telegram(msg)
        if alerted:
            state["last_alert_level"] = level
            state["last_alert_ts"] = now_iso()
            save_state(state)
    elif level < THRESHOLD:
        # Reseteo cuando baja, para volver a poder avisar en el proximo pico.
        if last_level != 0:
            state["last_alert_level"] = 0
            save_state(state)

    log_row([now_iso(), "dashboard", level, airborne, expected, alerted, ""])
    print("nivel={} airborne={} expected={} alerted={}".format(
        level, airborne, expected, alerted))


# ----------------------------- Lectura del RSS -----------------------------
def check_rss():
    """Modo rss: el feed solo trae items cuando hay nivel 5."""
    raw = http_get(RSS_URL)
    root = ET.fromstring(raw)

    item = root.find(".//item")
    if item is None:
        log_row([now_iso(), "rss", "", "", "", False, "sin items (nivel < 5)"])
        print("Sin items en el feed: no hay alerta de nivel 5.")
        return

    def txt(tag):
        el = item.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    guid = txt("guid") or txt("pubDate") or txt("title")
    title = txt("title")
    link = txt("link") or "https://ews.kylemcdonald.net/"

    state = load_state()
    alerted = False
    if guid and guid != state.get("last_rss_guid", ""):
        msg = "EWS - ALERTA NIVEL 5\n{}\n{}".format(title, link)
        alerted = send_telegram(msg)
        if alerted:
            state["last_rss_guid"] = guid
            state["last_alert_ts"] = now_iso()
            save_state(state)

    log_row([now_iso(), "rss", 5, "", "", alerted, title])
    print("ultimo item rss: {!r} alerted={}".format(title, alerted))


# ----------------------------------- Main ----------------------------------
def main():
    try:
        if MODE == "dashboard":
            check_dashboard()
        else:
            check_rss()
        if HEARTBEAT:
            send_telegram(
                "EWS activo - latido semanal -.\n"
                "El monitor esta funcionando; te avisaremos si hay nivel 5.\n"
                
            )
            print("latido enviado")
    except urllib.error.HTTPError as e:
        print("ERROR HTTP {}: {}".format(e.code, e.reason), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print("ERROR de red:", e.reason, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
