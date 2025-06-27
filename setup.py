#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Interaktives Setup-Tool für den KNX Logger und Explorer.
- Findet Gateways, fragt nach der Konfiguration und speichert sie in einer .env-Datei.
- Erfragt Pfade für Log-Dateien und die ETS-Projektdatei.
"""

import asyncio
import logging
import sys
import os
import re
from pathlib import Path
from typing import List, Tuple, Optional

# Setup-spezifische Imports
from dotenv import set_key, find_dotenv
try:
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.screen import Screen, ModalScreen
    from textual.widgets import Button, Header, Footer, Label, LoadingIndicator, RadioButton, RadioSet, Input
    TEXTUAL_INSTALLED = True
except ImportError:
    TEXTUAL_INSTALLED = False

# XKNX-Imports
from xknx import XKNX
from xknx.io import GatewayScanner
from xknx.exceptions import XKNXException
from xknx.io.gateway_scanner import GatewayDescriptor


# --- Setup-TUI-Komponenten ---

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
    """Fragt nach einem Pfad für Log-Dateien oder Projektdateien."""
    def __init__(self, prompt: str, default_path: str, info_text: str):
        super().__init__()
        self.prompt = prompt
        self.default_path = default_path
        self.info_text = info_text

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(
            Label(self.prompt, id="question"),
            Input(self.default_path, id="path_input"),
            Label(self.info_text, classes="info-label"),
            Button("Speichern und weiter", variant="primary", id="confirm_path"),
            id="dialog",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        path = self.query_one(Input).value
        self.dismiss(path)

class PasswordScreen(Screen[str]):
    """Fragt nach dem Passwort für die Projektdatei."""
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(
            Label("Passwort für die .knxproj-Datei (leer lassen, wenn keins)", id="question"),
            Input(placeholder="Projektpasswort...", password=True, id="password_input"),
            Button("Speichern und weiter", variant="primary", id="confirm_password"),
            id="dialog",
        )
        yield Footer()
    
    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value)

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

class LoadingScreen(Screen[Tuple[List[GatewayDescriptor], Optional[Exception]]]):
    """Zeigt einen Ladeindikator während des Scans."""
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(
            Label("Suche nach KNX-Gateways im Netzwerk...", classes="centered-label"),
            LoadingIndicator(),
            Label("(Scan läuft für maximal 12 Sekunden)", classes="info-label"),
            id="dialog"
        )

    async def on_mount(self) -> None:
        self.app.log("Starte KNX Gateway-Suche...")
        gateways: List[GatewayDescriptor] = []
        error: Optional[Exception] = None
        try:
            scanner = GatewayScanner(XKNX(), timeout_in_seconds=12)
            gateways = await scanner.scan()
            self.app.log(f"Scan beendet. Gesammelte Gateways: {gateways}")
        except Exception as e:
            logging.exception("Ein Fehler ist bei der Gateway-Suche aufgetreten:")
            error = e
        self.dismiss(result=(gateways, error))

class SelectionScreen(Screen[str]):
    """Ermöglicht die Auswahl eines gefundenen Gateways."""
    def __init__(self, gateways: List[GatewayDescriptor]):
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

class ResultScreen(Screen[None]):
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
        self.app.exit()

class SetupApp(App):
    """Die Textual-App für das interaktive Setup."""
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
            # .env-Datei finden oder erstellen
            dotenv_path = find_dotenv()
            if not dotenv_path:
                logging.warning("Keine .env-Datei gefunden, erstelle eine neue.")
                env_file_path = Path(".env")
                env_file_path.touch()
                dotenv_path = str(env_file_path.resolve())

            # 1. Gateway-Modus abfragen
            selection_mode = await self.push_screen_wait(SelectionModeScreen())
            logging.info(f"Gateway-Modus ausgewählt: {selection_mode}")

            # 2. Pfade abfragen
            log_path = await self.push_screen_wait(
                PathScreen("In welchem Verzeichnis sollen die Log-Dateien gespeichert werden?", os.getcwd(), "Der Pfad wird erstellt, falls er nicht existiert.")
            )
            logging.info(f"Log-Pfad ausgewählt: {log_path}")
            set_key(dotenv_path, "LOG_PATH", log_path)

            knx_project_path = await self.push_screen_wait(
                PathScreen("Pfad zur .knxproj-Datei:", "", "Kann ein relativer oder absoluter Pfad sein.")
            )
            logging.info(f"KNX-Projekt-Pfad ausgewählt: {knx_project_path}")
            set_key(dotenv_path, "KNX_PROJECT_PATH", knx_project_path)
            
            # 3. Passwort abfragen
            knx_password = await self.push_screen_wait(PasswordScreen())
            logging.info("KNX-Passwort eingegeben.")
            set_key(dotenv_path, "KNX_PASSWORD", knx_password)


            # 4. Gateway-spezifische Logik
            if selection_mode == "auto":
                set_key(dotenv_path, "KNX_GATEWAY_IP", "AUTO")
                set_key(dotenv_path, "KNX_GATEWAY_PORT", "0")
                logging.info("Konfiguration für AUTO-Modus in .env gespeichert.")
                await self.push_screen(ResultScreen("✅ Setup erfolgreich!\n\nDie Konfiguration wurde in '.env' gespeichert."))
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
                        await self.push_screen(ResultScreen(f"✅ Setup erfolgreich!\n\nAlle Einstellungen wurden in '.env' gespeichert."))
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


def main():
    """Startpunkt der Setup-Anwendung."""
    # Basic logging setup for the application's debug log
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("knx_setup_debug.log", mode='w', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    if not TEXTUAL_INSTALLED:
        logging.error("Für das interaktive Setup müssen die Pakete 'textual' und 'python-dotenv' installiert sein.")
        logging.error("Bitte führen Sie aus: pip install textual python-dotenv")
        sys.exit(1)
    
    logging.info("Starte interaktives Setup...")
    app = SetupApp()
    app.run()
    logging.info("Setup-Prozess beendet.")
    print("\nFühren Sie 'knx_logger.py' zum Loggen oder 'knx_lens.py' zum Analysieren aus.")

if __name__ == "__main__":
    main()
