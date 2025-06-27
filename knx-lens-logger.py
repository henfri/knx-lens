#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ein Python-Tool zum Loggen des KNX-Busverkehrs.
- Liest die Konfiguration aus einer .env-Datei.
- Loggt in eine rotierende Log-Datei im benutzerdefinierten Pfad.
- Komprimiert alte Logs um Mitternacht automatisch mit ZIP.
- Dekodiert Payloads, wenn eine ETS-Projektdatei konfiguriert ist.
- Schreibt alle Schritte und Fehler in eine dedizierte Debug-Logdatei.
"""

import asyncio
import logging
import sys
import os
import zipfile
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Third-party imports
from dotenv import load_dotenv

# XKNX-Imports
from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType, GatewayScanner
from xknx.telegram import Telegram
from xknx.telegram.apci import GroupValueWrite, GroupValueResponse
from xknxproject.models import KNXProject


# --- LOGGING-KONFIGURATION ---

class ZipTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    Handler for rotating logs with ZIP compression for better Windows compatibility.
    """
    def rotator(self, source: str, dest: str) -> None:
        """
        Compresses the source log file into a zip archive.
        The destination path from the base class already includes the timestamp.
        """
        zip_path = f"{dest}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                # Add the source file to the zip, using just the filename inside the archive
                zf.write(source, os.path.basename(source))
            os.remove(source)
            logging.info(f"Log file rotated and compressed to {zip_path}")
        except Exception as e:
            # Using print as logging might be part of the problem
            print(f"Error during log rotation to ZIP: {e}", file=sys.stderr)
            logging.exception("Fehler bei der Log-Rotation zu ZIP")


def setup_knx_bus_logger(log_path: str, is_daemon_mode: bool) -> logging.Logger:
    """Konfiguriert den Logger für den reinen KNX-Busverkehr."""
    log_dir = Path(log_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "knx_bus.log"

    bus_logger = logging.getLogger("knx_bus_logger")
    bus_logger.setLevel(logging.INFO)
    bus_logger.propagate = False  # Verhindert, dass Logs zum Root-Logger gelangen

    if bus_logger.hasHandlers():
        bus_logger.handlers.clear()

    formatter = logging.Formatter('%(message)s')
    
    # 1. Handler: Loggt immer in die rotierende Datei
    file_handler = ZipTimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,  # Keep 30 days of logs
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    bus_logger.addHandler(file_handler)

    # 2. Handler: Loggt NUR auf die Konsole, wenn nicht im Daemon-Modus
    if not is_daemon_mode:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        bus_logger.addHandler(console_handler)
        
    return bus_logger


# --- TELEGRAMM-VERARBEITUNG ---

def telegram_to_log_message(telegram: Telegram, knx_project: Optional[KNXProject]) -> str:
    """Formatiert ein Telegramm in eine menschenlesbare Log-Zeile."""
    ia_string = str(telegram.source_address)
    ga_string = str(telegram.destination_address)
    payload: Any
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    ia_name = ""
    ga_name = ""
    data_str = ""

    if isinstance(telegram.payload, (GroupValueWrite, GroupValueResponse)):
        payload = telegram.payload.value.value if telegram.payload.value else "None"
    else:
        # Fallback für andere Payload-Typen
        payload = str(telegram.payload)

    if knx_project:
        if (device := knx_project["devices"].get(ia_string)) is not None:
            ia_name = device.get('name', '')
        if (ga_data := knx_project["group_addresses"].get(ga_string)) is not None:
            ga_name = ga_data.get('name', '')
        
        if (data := telegram.decoded_data) is not None:
            # Prüfen, ob das 'unit'-Attribut existiert, um AttributeError zu vermeiden
            if hasattr(data, 'unit') and data.unit is not None:
                data_str = f"{data.value} {data.unit}".strip()
            else:
                data_str = str(data.value)
        else:
            # Fallback, wenn keine dekodierten Daten vorhanden sind
            data_str = str(payload)
            
    else:
        # Ohne Projektdatei nur die Rohdaten verwenden
        data_str = str(payload)
        
    # ANGEPASSTE SPALTENBREITEN ZUR VERMEIDUNG VON ZEILENUMBRÜCHEN
    col_widths = {
        "timestamp": 26,
        "ia_string": 9,
        "ia_name": 25,  # Gekürzt von 30
        "ga_string": 8,
        "ga_name": 30,  # Gekürzt von 34
        "data": 25      # Gekürzt von 50
    }
    # Erzeugt eine saubere, mit Pipe getrennte Zeile
    line = (
        f"{timestamp:<{col_widths['timestamp']}} | "
        f"{ia_string[:col_widths['ia_string']]:<{col_widths['ia_string']}} | "
        f"{ia_name[:col_widths['ia_name']]:<{col_widths['ia_name']}} | "
        f"{ga_string[:col_widths['ga_string']]:<{col_widths['ga_string']}} | "
        f"{ga_name[:col_widths['ga_name']]:<{col_widths['ga_name']}} | "
        f"{data_str[:col_widths['data']]:<{col_widths['data']}}"
    )
    return line

def load_project(file_path: str, password: Optional[str]) -> Optional[KNXProject]:
    """Lädt ein KNX-Projekt aus einer Datei."""
    try:
        from xknxproject import XKNXProj
        from xknxproject.exceptions import InvalidPasswordException
    except ImportError:
        logging.error("xknxproject ist nicht installiert. Bitte mit 'pip install xknxproject' installieren.")
        return None

    if not Path(file_path).is_file():
        logging.warning(f"ETS-Projektdatei nicht unter '{file_path}' gefunden. Telegramme werden nicht dekodiert.")
        return None

    try:
        xknxproj = XKNXProj(file_path, password=password)
        logging.info(f"Lade ETS-Projekt '{file_path}'...")
        project = xknxproj.parse()
        logging.info(f"ETS-Projekt '{file_path}' erfolgreich geladen.")
        return project
    except InvalidPasswordException:
        logging.error(f"Ungültiges Passwort für die ETS-Projektdatei '{file_path}'.")
        return None
    except Exception as e:
        logging.error(f"Fehler beim Laden des ETS-Projekts '{file_path}': {e}. Telegramme werden nicht dekodiert.")
        return None


def telegram_received_cb(telegram: Telegram, knx_project: Optional[KNXProject], logger: logging.Logger):
    """Callback, der bei jedem Telegramm aufgerufen wird."""
    log_message = telegram_to_log_message(telegram, knx_project)
    logger.info(log_message)

async def start_logger_mode():
    """Stellt eine Verbindung zum KNX-Bus her und loggt alle Telegramme."""
    load_dotenv()
    is_daemon_mode = '--daemon' in sys.argv

    knx_ip = os.getenv("KNX_GATEWAY_IP")
    knx_port = os.getenv("KNX_GATEWAY_PORT")
    log_path = os.getenv("LOG_PATH", ".")
    ets_project_file = os.getenv("KNX_PROJECT_PATH")
    ets_password = os.getenv("KNX_PASSWORD")

    # Logger wird jetzt mit dem Daemon-Status initialisiert
    bus_logger = setup_knx_bus_logger(log_path, is_daemon_mode)

    if not is_daemon_mode:
        print("\n" + "=" * 50)
        print("Starte den KNX Logger...")

    logging.info("=" * 50)
    logging.info("Starte den KNX Logger...")
    bus_logger.info("=" * 80)
    bus_logger.info(f"Logger gestartet am {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    bus_logger.info("=" * 80)

    connection_config: ConnectionConfig
    if knx_ip == "AUTO":
        logging.info("Suche nach einem automatischen Gateway...")
        try:
            async with XKNX() as xknx_for_scan:
                scanner = GatewayScanner(xknx_for_scan)
                gateways = await scanner.scan()
                if not gateways:
                    logging.error("Kein Gateway im 'AUTO'-Modus gefunden. Beende.")
                    return
                gateway = gateways[0]
                logging.info(f"Gateway gefunden: {gateway.name} ({gateway.ip_addr}:{gateway.port})")
                connection_config = ConnectionConfig(gateway_ip=gateway.ip_addr, gateway_port=gateway.port)
        except Exception:
            logging.exception("Fehler bei der automatischen Gateway-Suche:")
            return
    elif not knx_ip or not knx_port:
        logging.error("Gateway-Informationen konnten nicht geladen werden. Bitte 'setup.py' ausführen.")
        return
    else:
        logging.info(f"Verwende konfiguriertes Gateway: {knx_ip}:{knx_port}")
        connection_config = ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=knx_ip, gateway_port=int(knx_port)
        )
    
    knx_project = load_project(ets_project_file, ets_password) if ets_project_file else None

    xknx = XKNX(connection_config=connection_config, daemon_mode=True)
    
    if knx_project:
        xknx.knxproj = knx_project
        
    if knx_project is not None:
        dpt_dict = {
            ga: data["dpt"]
            for ga, data in knx_project["group_addresses"].items()
            if data["dpt"] is not None
        }
        xknx.group_address_dpt.set(dpt_dict)
    
    # Callback-Registrierung vereinfacht, da is_daemon_mode nicht mehr benötigt wird
    xknx.telegram_queue.register_telegram_received_cb(
        lambda t: telegram_received_cb(t, knx_project, bus_logger)
    )

    if not is_daemon_mode:
        print("Verbindung wird hergestellt... Warte auf Telegramme.")
        print("Drücken Sie Strg+C zum Beenden.")
        print("=" * 50)

    try:
        await xknx.start()
        await xknx.stop() # This will be reached on clean shutdown.
    except asyncio.CancelledError:
        logging.info("Asyncio-Task wurde abgebrochen, wahrscheinlich durch Strg+C.")
    except Exception:
        logging.exception("Ein unerwarteter Fehler ist während der KNX-Verbindung aufgetreten:")
    finally:
        logging.info("Logger wird beendet...")
        if xknx.started:
            await xknx.stop()
        logging.info("Aufgeräumt. Programm beendet.")

def main():
    """Startpunkt der Anwendung."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("knx_logger_debug.log", mode='w', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    load_dotenv()
    knx_ip_from_env = os.getenv("KNX_GATEWAY_IP")
    if not knx_ip_from_env:
        print("Keine Konfiguration gefunden. Bitte führen Sie zuerst 'python setup.py' aus.", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(start_logger_mode())
    except KeyboardInterrupt:
        logging.info("Programm wurde durch Benutzer (Strg+C) beendet.")
    except Exception:
        logging.exception("Ein unerwarteter Fehler hat die Anwendung beendet:")
    
    logging.info("Anwendung heruntergefahren.")

if __name__ == "__main__":
    main()
