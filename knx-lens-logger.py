#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ein Python-Tool zum Loggen des KNX-Busverkehrs mit interaktivem Setup.
- Findet Gateways, fragt nach Konfiguration und speichert sie in einer .env-Datei.
- Loggt in eine rotierende Log-Datei im benutzerdefinierten Pfad.
- Dekodiert Payloads mithilfe einer ETS-Projektdatei.
- Alte Logs werden um Mitternacht automatisch mit gzip komprimiert.
- Schreibt alle Schritte und Fehler in eine dedizierte Debug-Logdatei.
"""

import asyncio
import logging
import sys
import os
import re
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
import gzip
from pathlib import Path
from typing import Any, TypeVar

# Setup-spezifische Imports
from dotenv import load_dotenv, set_key, find_dotenv
try:
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll, Horizontal
    from textual.screen import Screen
    from textual.widgets import Button, Header, Footer, Label, LoadingIndicator, RadioButton, RadioSet, Input
    TEXTUAL_INSTALLED = True
except ImportError:
    TEXTUAL_INSTALLED = False

# XKNX-Imports
from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType, GatewayScanner
from xknx.telegram import Telegram, TelegramDirection
from xknx.exceptions import XKNXException
from xknx.io.gateway_scanner import GatewayDescriptor
from xknx.telegram.apci import GroupValueWrite, GroupValueResponse
from xknx.io.connection import ConnectionConfig
from xknx.telegram import AddressFilter, IndividualAddress, Telegram

# Import KNXProject unconditionally so it's available at runtime for type hints.
# The `TYPE_CHECKING` block is only for static type checkers and is False at runtime.
from xknxproject.models import KNXProject

# Define a TypeVar for the KNXProject for better type hinting
_KNXProject = TypeVar("_KNXProject", bound=KNXProject)


# --- SETUP-TEIL (benötigt 'textual' und 'python-dotenv') ---

if TEXTUAL_INSTALLED:
    class SelectionModeScreen(Screen[str]):
        """Fragt, ob ein Gateway gespeichert oder bei jedem Start gesucht werden soll."""
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield VerticalScroll(
                Label("Konfiguration des Gateways", id="question"),
                RadioSet(
                    RadioButton("Ein bestimmtes Gateway suchen und speichern (empfohlen)", id="save"),
                    RadioButton("Bei jedem Start automatisch nach einem Gateway suchen", id="auto"),
                    id="mode_options"
                ),
                Button("Weiter", variant="primary", id="confirm_mode"),
                id="dialog",
            )
            yield Footer()
        def on_mount(self) -> None:
            self.query_one(RadioSet).focus()
            self.query_one("#save").value = True
        def on_button_pressed(self, event: Button.Pressed) -> None:
            if self.query_one("#save").value:
                self.dismiss("save")
            else:
                self.dismiss("auto")

    class PathScreen(Screen[str]):
        """Fragt nach dem Pfad für die Log-Dateien."""
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield VerticalScroll(
                Label("In welchem Verzeichnis sollen die Log-Dateien gespeichert werden?", id="question"),
                Input(os.getcwd(), id="path_input"),
                Label("Der Pfad wird erstellt, falls er nicht existiert.", classes="info-label"),
                Button("Speichern und weiter", variant="primary", id="confirm_path"),
                id="dialog",
            )
            yield Footer()
        def on_mount(self) -> None:
            self.query_one(Input).focus()
        def on_button_pressed(self, event: Button.Pressed) -> None:
            path = self.query_one(Input).value
            self.dismiss(path)

    class StartScreen(Screen[bool]):
        """Startbildschirm für den Gateway-Scan."""
        BINDINGS = [("q", "request_quit", "Beenden")]
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield VerticalScroll(
                Label("Willkommen beim interaktiven Setup.", id="question"),
                Label("Möchten Sie jetzt nach KNX-Gateways suchen?"),
                Button("Scan starten", variant="primary", id="start_scan"),
                Button("Abbrechen", variant="default", id="cancel"),
                id="dialog",
            )
            yield Footer()
        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "start_scan")
        def action_request_quit(self) -> None:
            self.app.exit(message="Setup wurde durch Benutzer beendet.")
    
    class LoadingScreen(Screen[tuple[list[GatewayDescriptor], Exception | None]]):
        """
        Zeigt einen Ladeindikator während des Scans und gibt das Ergebnis
        oder eine Exception zurück. Verwendet die moderne Scan-Methode.
        """
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield VerticalScroll(
                Label("Suche nach KNX-Gateways im Netzwerk...", classes="centered-label"),
                LoadingIndicator(),
                Label("(Scan läuft für maximal 12 Sekunden)", classes="info-label"),
                id="dialog"
            )

        async def on_mount(self) -> None:
            self.app.log("Starte KNX Gateway-Suche mit moderner Methode...")
            gateways: list[GatewayDescriptor] = []
            error: Exception | None = None
            try:
                scanner = GatewayScanner(XKNX(), timeout_in_seconds=12)
                gateways = await scanner.scan()
                self.app.log(f"Scan beendet. Gesammelte Gateways: {gateways}")
            except Exception as e:
                logging.exception("Ein allgemeiner Fehler ist bei der Gateway-Suche aufgetreten:")
                error = e
            self.dismiss(result=(gateways, error))

    class SelectionScreen(Screen[str]):
        """Ermöglicht die Auswahl eines gefundenen Gateways."""
        def __init__(self, gateways: list[GatewayDescriptor]):
            super().__init__()
            self.gateways = gateways
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            gateway_labels = [f"{gw.name} ({gw.ip_addr}:{gw.port})" for gw in self.gateways]
            radio_buttons = [RadioButton(label=gw, id=f"gateway_{i}") for i, gw in enumerate(gateway_labels)]
            yield VerticalScroll(
                Label("Bitte wählen Sie ein Gateway aus:", id="question"),
                RadioSet(*radio_buttons, id="gateway_options"),
                Button("Bestätigen", variant="primary", id="confirm_selection"),
                id="dialog"
            )
            yield Footer()
        def on_mount(self) -> None:
            self.query_one(RadioSet).focus()
            if self.query(RadioButton):
                self.query(RadioButton).first().value = True
        def on_button_pressed(self, event: Button.Pressed) -> None:
            radioset = self.query_one(RadioSet)
            if radioset.pressed_button:
                self.dismiss(result=radioset.pressed_button.label.plain)

    class ResultScreen(Screen):
        """Zeigt das Endergebnis des Setups an."""
        def __init__(self, message: str):
            super().__init__()
            self.message = message
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield VerticalScroll(
                Label(self.message, id="result_message"),
                Button("Beenden", variant="primary", id="quit_app"),
                id="dialog",
            )
            yield Footer()
        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.app.exit(result=self.message)
    
    class SetupApp(App):
        """Die Textual-App für das interaktive Setup."""
        CSS_PATH = None
        CSS = """
        Screen { align: center middle; }
        #dialog { padding: 1 2; width: 80; max-width: 90%; height: auto; border: round #666; background: $surface; }
        #question { width: 100%; text-align: center; margin-bottom: 1; }
        Button { width: 100%; }
        Input { width: 100%; margin-bottom: 1; }
        #result_message { width: 100%; text-align: center; margin: 1 0; }
        .centered-label { text-align: center; width: 100%; margin: 1 0; }
        .info-label { color: $text-muted; text-align: center; width: 100%; }
        RadioSet { margin: 1 0; }
        """
        BINDINGS = [("q", "request_quit", "Beenden")]

        def on_mount(self) -> None:
            logging.info("Setup-App gestartet.")
            self.run_worker(self.run_setup_flow, exclusive=True)
            
        async def run_setup_flow(self) -> None:
            try:
                selection_mode = await self.push_screen_wait(SelectionModeScreen())
                logging.info(f"Gateway-Modus ausgewählt: {selection_mode}")

                log_path = await self.push_screen_wait(PathScreen())
                logging.info(f"Log-Pfad ausgewählt: {log_path}")

                dotenv_path = find_dotenv()
                if not dotenv_path:
                    logging.warning("Keine .env-Datei gefunden, erstelle eine neue.")
                    with open(".env", "w") as f: f.write("")
                    dotenv_path = find_dotenv()
                set_key(dotenv_path, "LOG_PATH", log_path)
                
                if selection_mode == "auto":
                    set_key(dotenv_path, "KNX_GATEWAY_IP", "AUTO")
                    set_key(dotenv_path, "KNX_GATEWAY_PORT", "0")
                    logging.info("Konfiguration für AUTO-Modus in .env gespeichert.")
                    await self.push_screen(ResultScreen("✅ Setup erfolgreich!\n\nDer Logger wird bei jedem Start automatisch nach einem Gateway suchen."))
                    return

                if selection_mode == "save":
                    start_scan = await self.push_screen_wait(StartScreen())
                    if not start_scan:
                        logging.warning("Gateway-Scan vom Benutzer abgebrochen.")
                        await self.push_screen(ResultScreen("Setup wurde abgebrochen."))
                        return
                    
                    gateways, error = await self.push_screen_wait(LoadingScreen())
                    if error:
                        logging.error("Fehler von LoadingScreen erhalten.")
                        await self.push_screen(ResultScreen(f"❌ Fehler bei der Gateway-Suche:\n{error}"))
                        return
                    if not gateways:
                        logging.warning("Keine Gateways gefunden.")
                        await self.push_screen(ResultScreen("❌ Es konnten keine Gateways gefunden werden."))
                        return
                    
                    selected_gateway_str: str
                    if len(gateways) == 1:
                        gw = gateways[0]
                        selected_gateway_str = f"{gw.name} ({gw.ip_addr}:{gw.port})"
                    else:
                        selected_gateway_str = await self.push_screen_wait(SelectionScreen(gateways))
                    
                    if selected_gateway_str:
                        logging.info(f"Gateway ausgewählt: {selected_gateway_str}")
                        match = re.search(r"\((\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)\)", selected_gateway_str)
                        if match:
                            ip_addr, port = match.groups()
                            set_key(dotenv_path, "KNX_GATEWAY_IP", ip_addr)
                            set_key(dotenv_path, "KNX_GATEWAY_PORT", port)
                            logging.info(f"Gateway {ip_addr}:{port} in .env gespeichert.")
                            await self.push_screen(ResultScreen(f"✅ Setup erfolgreich!\n\nGateway wurde in der Datei '.env' gespeichert."))
                        else:
                            logging.error(f"Konnte IP/Port nicht aus '{selected_gateway_str}' extrahieren.")
                            await self.push_screen(ResultScreen("Fehler: IP/Port konnten nicht extrahiert werden."))
                    else:
                        logging.warning("Kein Gateway in SelectionScreen bestätigt.")
                        await self.push_screen(ResultScreen("❌ Es wurde kein Gateway ausgewählt."))
            except Exception as e:
                logging.exception("Ein unerwarteter Fehler ist im Setup-Flow aufgetreten:")
                await self.push_screen(ResultScreen(f"Ein schwerwiegender Fehler ist aufgetreten. Details siehe 'knx_app_debug.log'."))

        def action_request_quit(self) -> None:
            logging.info("Setup vom Benutzer über 'q' beendet.")
            self.exit(message="Setup wurde durch Benutzer beendet.")

# --- LOGGER-TEIL: Die eigentliche Anwendung ---

class GzipTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Handler for rotating logs with gzip compression."""
    def rotator(self, source: str, dest: str) -> None:
        try:
            with open(source, "rb") as sf:
                with gzip.open(f"{dest}.gz", "wb") as df:
                    df.writelines(sf)
            os.remove(source)
        except Exception as e:
            print(f"Error during log rotation: {e}", file=sys.stderr)
            logging.exception("Fehler bei der Log-Rotation")

def setup_knx_bus_logger(log_path: str) -> logging.Logger:
    """Konfiguriert den Logger für den reinen KNX-Busverkehr."""
    log_dir = Path(log_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "knx_bus.log"

    bus_logger = logging.getLogger("knx_bus_logger")
    bus_logger.setLevel(logging.INFO)
    bus_logger.propagate = False  # Prevent logs from going to root logger

    if bus_logger.hasHandlers():
        bus_logger.handlers.clear()

    formatter = logging.Formatter('%(message)s')
    handler = GzipTimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    handler.setFormatter(formatter)
    bus_logger.addHandler(handler)
    return bus_logger

def _serializable_decoded_data(value: Any) -> Any:
    """Formatiert komplexe dekodierte Daten für die Ausgabe (z. B. Tupel/Listen)."""
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    return value

def telegram_to_log_message(telegram: Telegram, knx_project: KNXProject) -> str:
    """Do something with the received telegram."""
    ia_string = str(telegram.source_address)
    ga_string = str(telegram.destination_address)
    payload: str | int | tuple[int, ...]
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    # will be filled only if knx_project
    ia_name = ""
    ga_name = ""
    data_str = ""
    if isinstance(telegram.payload, GroupValueWrite | GroupValueResponse):
        payload = telegram.payload.value.value
    else:
        payload = str(telegram.payload.__class__.__name__)
    if knx_project:
        if (device := knx_project["devices"].get(ia_string)) is not None:
            ia_name = f"{device['name']}"
            #ia_address = f"{device['individual_address']}"
        if (ga_data := knx_project["group_addresses"].get(ga_string)) is not None:
            ga_name = ga_data["name"]
            #ga_address = ga_data["address"]         
        if (data := telegram.decoded_data) is not None:
            data_str = f"{data.value} {data.transcoder.unit or ''}"

        log_message = f"{timestamp} | {ia_string[:9]:9} |{ia_name[:30]:30} | {ga_string[:8]:8} | {ga_name[:34]:34}| {data_str}"
    else:
        # no project file
        log_message = f"{timestamp} | {ia_string[:9]:9} |{ia_name[:30]:30} | {ga_string[:8]:8} | {ga_name[:34]:34}| {payload}"


    return log_message


def load_project(file_path: str, password: str) -> KNXProject:
    """Load KNX project from file."""
    # pylint: disable=import-outside-toplevel
    try:
        from xknxproject import XKNXProj
        from xknxproject.exceptions import InvalidPasswordException
    except ImportError:
        print(
            "xknxproject package is not installed. Please install it with 'pip install xknxproject'."
        )
        sys.exit(1)

    xknxproj = XKNXProj(file_path)
    try:
        # xknxproject.parse() returns an instance of KNXProject
        return xknxproj.parse()
    except InvalidPasswordException:
        xknxproj.password = password
        return xknxproj.parse()


def telegram_received_cb(telegram: Telegram, knx_project: KNXProject, logger: logging.Logger, is_daemon_mode: bool):
    """Callback, der bei jedem Telegramm aufgerufen wird."""
    log_message = telegram_to_log_message(telegram, knx_project)
    logger.info(log_message)
    if not is_daemon_mode:
        print(log_message)

async def start_logger_mode():
    """Stellt eine Verbindung zum KNX-Bus her und loggt alle Telegramme."""
    load_dotenv()
    is_daemon_mode = '--daemon' in sys.argv
    
    knx_ip   = os.getenv("KNX_GATEWAY_IP")
    knx_port = os.getenv("KNX_GATEWAY_PORT")
    log_path = os.getenv("LOG_PATH", ".")
    ets_project_file = os.getenv("KNX_PROJECT_PATH") 
    ets_password = os.getenv("KNX_PASSWORD")

    bus_logger = setup_knx_bus_logger(log_path)
    
    if not is_daemon_mode:
        print("\n" + "="*50)
        print("Starte den KNX Logger...")
    
    logging.info("="*50)
    logging.info("Starte den KNX Logger...")
    bus_logger.info("="*80)
    bus_logger.info(f"Logger gestartet am {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    bus_logger.info("="*80)

    connection_config: ConnectionConfig
    if knx_ip == "AUTO":
        logging.info("Suche nach einem automatischen Gateway...")
        try:
            scanner = GatewayScanner(XKNX())
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
        logging.error("Gateway-Informationen konnten nicht geladen werden. Bitte '--setup' ausführen.")
        return
    else:
        logging.info(f"Verwende konfiguriertes Gateway: {knx_ip}:{knx_port}")
        connection_config = ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=knx_ip, gateway_port=int(knx_port)
        )
    

    if not Path(ets_project_file).is_file():
        logging.warning(f"ETS project file not found at '{ets_project_file}'. Telegrams will not be decoded.")
        knx_project = None # Set to None if file not found to avoid errors
    else:
        try:
            knx_project = load_project(ets_project_file, password=ets_password)
            logging.info(f"ETS project '{ets_project_file}' successfully loaded.")
        except Exception as e:
            logging.error(f"Failed to load ETS project '{ets_project_file}': {e}. Telegrams will not be decoded.")
            knx_project = None


    xknx = XKNX(
        connection_config=connection_config,
        daemon_mode=True #
    )
    if knx_project is not None:
        dpt_dict = {
            ga: data["dpt"]
            for ga, data in knx_project["group_addresses"].items()
            if data["dpt"] is not None
        }
        xknx.group_address_dpt.set(dpt_dict)
    

    xknx.telegram_queue.register_telegram_received_cb(
        lambda t: telegram_received_cb(t, knx_project, bus_logger, is_daemon_mode)
    )
    
    if not is_daemon_mode:
        print("Verbindung wird hergestellt... Warte auf Telegramme.")
        print("Drücken Sie Strg+C zum Beenden.")
        print("="*50)

    try:
        # ANWEISUNG UMGESETZT: start() wird ohne Argumente aufgerufen
        # In daemon_mode=True, xknx.start() will block until stopped.
        await xknx.start()
        # The stop_event.wait() is not strictly necessary here if daemon_mode=True
        # because xknx.start() already blocks. However, for explicit control or
        # if daemon_mode could be False, it helps manage the event loop.
        # For simplicity and to match common patterns, we keep it as an explicit wait point.
        # stop_event = asyncio.Event()
        # await stop_event.wait()
    except asyncio.CancelledError:
        logging.info("Asyncio task cancelled, likely due to KeyboardInterrupt.")
    except Exception as e:
        logging.exception("An unexpected error occurred during KNX connection or logging:")
    finally:
        logging.info("Logger wird beendet...")
        if xknx.started:
            await xknx.stop()
        logging.info("Aufgeräumt. Programm beendet.")

# --- Startpunkt der Anwendung ---

if __name__ == "__main__":
    # Basic logging setup for the application's debug log
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("knx_app_debug.log", mode='w', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    if not TEXTUAL_INSTALLED and '--setup' in sys.argv:
        logging.error("Für das interaktive Setup müssen die Pakete 'textual' und 'python-dotenv' installiert sein.")
        sys.exit(1)

    load_dotenv()
    knx_ip_from_env = os.getenv("KNX_GATEWAY_IP")
    run_setup = '--setup' in sys.argv or not knx_ip_from_env

    try:
        if run_setup and TEXTUAL_INSTALLED:
            logging.info("Starte interaktives Setup...")
            app = SetupApp()
            app.run()
            logging.info("Setup-Prozess beendet.")
            print("\nStarten Sie das Skript erneut ohne '--setup', um den Logger zu verwenden.")
        else:
            asyncio.run(start_logger_mode())
    except KeyboardInterrupt:
        logging.info("Programm wurde durch Benutzer (Strg+C) beendet.")
    except Exception:
        logging.exception("Ein unerwarteter Fehler hat die Anwendung beendet:")
            
    logging.info("Anwendung heruntergefahren.")
