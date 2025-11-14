#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TUI-Bildschirme und Widgets für KNX-Lens.
"""

import logging # <-- WICHTIG
from typing import Tuple, Optional, Iterable
from pathlib import Path
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Label, Input, Button, DirectoryTree
from textual.containers import Vertical, Center, Horizontal
from textual import events

# --- DEBUG: Gefilterter Dateibaum mit Logging ---
class FilteredDirectoryTree(DirectoryTree):
    """Ein Dateibaum, der nur relevante Dateien (.log, .zip, .txt, .knxproj) anzeigt."""
    
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        # Iterator in Liste umwandeln, damit wir loggen können
        path_list = list(paths)
        
        filtered = [
            path for path in path_list 
            if path.is_dir() or 
               path.name.lower().endswith((".log", ".zip", ".txt", ".knxproj"))
        ]
        
        # Nur loggen, wenn wir tatsächlich Dateien filtern (verhindert Spam bei leerem Verzeichnis)
        if path_list:
            logging.debug(f"DirectoryTree Filter: {len(path_list)} Objekte gefunden, {len(filtered)} behalten.")
            # Optional: Zeige die ersten paar behaltenen Dateien im Log
            if filtered:
                logging.debug(f"  -> Beispiele: {[p.name for p in filtered[:3]]}")
        else:
            logging.debug("DirectoryTree Filter: Verzeichnis scheint leer zu sein.")

        return filtered

class FilterInputScreen(ModalScreen[str]):
    """Ein modaler Bildschirm für die Filtereingabe."""
    
    # --- KORREKTUR: __init__ hinzugefügt ---
    def __init__(self, prompt: str = "Baum filtern (Enter zum Bestätigen, ESC zum Abbrechen):"):
        """
        Initialisiert den Screen mit einem benutzerdefinierten Prompt.
        
        Args:
            prompt: Der Text, der über dem Eingabefeld angezeigt wird.
        """
        super().__init__()
        self.prompt = prompt
    # --- ENDE KORREKTUR ---

    def compose(self) -> ComposeResult:
        yield Center(Vertical(
            # --- KORREKTUR: Hardcodierter Text durch Variable ersetzt ---
            Label(self.prompt),
            Input(placeholder="Eingabe...", id="filter_input"),
            id="filter_dialog"
        ))
        
    def on_mount(self) -> None: 
        self.query_one("#filter_input", Input).focus()
        
    def on_input_submitted(self, event: Input.Submitted) -> None: 
        # Hack für "Ja/Nein"-Dialoge
        if "ja/nein" in self.prompt.lower():
            if event.value.lower() in ["ja", "j", "yes", "y"]:
                self.dismiss("ja")
            else:
                self.dismiss("") # Alles andere ist "Nein"
        else:
            self.dismiss(event.value)
            
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape": 
            self.dismiss("")

class TimeFilterScreen(ModalScreen[Tuple[Optional[str], Optional[str]]]):
    """Ein modaler Bildschirm für den Zeitfilter."""
    
    def __init__(self, start_val: Optional[str], end_val: Optional[str]):
        super().__init__()
        self.start_val = start_val or ""
        self.end_val = end_val or ""

    def compose(self) -> ComposeResult:
        yield Center(Vertical(
            Label("Log nach Zeit filtern (z.B. 10:30):"),
            Label("Leer lassen zum Deaktivieren."),
            Input(placeholder="Startzeit (HH:MM)", id="start_input", value=self.start_val),
            Input(placeholder="Endzeit (HH:MM)", id="end_input", value=self.end_val),
            Horizontal(
                Button("Filtern", variant="success", id="apply_filter"),
                Button("Abbrechen", variant="error", id="cancel"),
            ),
            id="time_filter_dialog" 
        ))
    
    def on_mount(self) -> None: 
        self.query_one("#start_input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss((None, None))
        elif event.button.id == "apply_filter":
            start = self.query_one("#start_input").value
            end = self.query_one("#end_input").value
            self.dismiss((start, end))