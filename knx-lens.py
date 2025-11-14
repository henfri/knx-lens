#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ein interaktiver KNX Projekt-Explorer und Log-Filter.
Hauptdatei: Initialisiert die App und verbindet Logik mit UI.
"""
import argparse
import os
import sys
import traceback
import logging
import time
import re
import zipfile
import io
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

# Third-party libraries
from dotenv import load_dotenv, set_key, find_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
# WICHTIG: Input widget importieren
from textual.widgets import Header, Footer, Tree, Static, TabbedContent, TabPane, DataTable, DirectoryTree, Input
from textual.widgets.tree import TreeNode
from textual import events
from textual.timer import Timer

# --- LOKALE IMPORTE ---
from knx_project_utils import (
    load_or_parse_project, 
    build_ga_tree_data, 
    build_pa_tree_data, 
    build_building_tree_data
)
# --- Import geändert ---
from knx_log_utils import parse_and_cache_log_data, append_new_log_lines
# Importiere Screens UND den neuen Tree
from knx_tui_screens import FilterInputScreen, TimeFilterScreen, FilteredDirectoryTree
# --- ENDE LOKALE IMPORTE ---


### --- SETUP & KONSTANTEN ---
LOG_LEVEL = logging.INFO
TreeData = Dict[str, Any]
MAX_LOG_LINES_NO_FILTER = 5000

### --- TUI: HAUPTANWENDUNG ---
class KNXLens(App):
    CSS_PATH = "knx-lens.css"
    BINDINGS = [
        Binding("q", "quit", "Beenden"),
        Binding("a", "toggle_selection", "Auswahl"),
        Binding("c", "copy_label", "Kopieren"),
        Binding("f", "filter_tree", "Filtern"),
        Binding("o", "open_log_file", "Dateien-Tab öffnen"),
        Binding("r", "reload_log_file", "Log neu laden"),
        Binding("t", "toggle_log_reload", "Auto-Reload Log"),
        Binding("i", "time_filter", "Zeitfilter"),
        Binding("escape", "reset_filter", "Auswahl zurücksetzen", show=True),
    ]

    def __init__(self, config: Dict):
        super().__init__()
        self.config = config
        self.project_data: Optional[Dict] = None
        self.building_tree_data: TreeData = {}
        self.pa_tree_data: TreeData = {}
        self.ga_tree_data: TreeData = {}
        self.selected_gas: Set[str] = set()
        
        self.log_widget: Optional[DataTable] = None
        self.log_reload_timer: Optional[Timer] = None
        self.payload_history: Dict[str, List[Dict[str, str]]] = {}
        self.cached_log_data: List[Dict[str, str]] = []
        
        self.time_filter_start: Optional[datetime_time] = None
        self.time_filter_end: Optional[datetime_time] = None

        # --- NEU: Regex-Filter Status ---
        self.regex_filter: Optional[re.Pattern] = None
        self.regex_filter_string: str = ""
        # --- ENDE NEU ---

        # --- NEU: Für effizientes Tailing ---
        self.last_log_mtime: Optional[float] = None
        self.last_log_position: int = 0
        # --- ENDE NEU ---


    def compose(self) -> ComposeResult:
        yield Header(name="KNX Projekt-Explorer")
        yield Vertical(Static("Lade und verarbeite Projektdatei...", id="loading_label"), id="loading_container")
        yield TabbedContent(id="main_tabs", disabled=True)
        yield Footer()

    def show_startup_error(self, exc: Exception, tb_str: str) -> None:
        try:
            loading_label = self.query_one("#loading_label")
            loading_label.update(f"[bold red]FEHLER BEIM LADEN[/]\n[yellow]Meldung:[/] {exc}\n\n[bold]Traceback:[/]\n{tb_str}")
        except Exception:
            logging.critical("Konnte UI-Fehler nicht anzeigen.", exc_info=True)

    def on_mount(self) -> None:
        self.run_worker(self.load_all_data, name="data_loader", thread=True)

    def load_all_data(self) -> None:
        try:
            self.project_data = load_or_parse_project(self.config['knxproj_path'], self.config['password'])
            self.ga_tree_data = build_ga_tree_data(self.project_data)
            self.pa_tree_data = build_pa_tree_data(self.project_data)
            self.building_tree_data = build_building_tree_data(self.project_data)
            self.call_from_thread(self.on_data_loaded)
        except Exception as e:
            self.call_from_thread(self.show_startup_error, e, traceback.format_exc())
    
    def on_data_loaded(self) -> None:
        logging.debug("on_data_loaded: Beginne mit UI-Aufbau.")
        try:
            logging.debug("on_data_loaded: Entferne Lade-Container.")
            loading_container = self.query_one("#loading_container")
            loading_container.remove()
            
            tabs = self.query_one(TabbedContent)
            
            building_tree = Tree("Gebäude", id="building_tree")
            pa_tree = Tree("Linien", id="pa_tree")
            ga_tree = Tree("Funktionen", id="ga_tree")
            
            self.log_widget = DataTable(id="log_view")
            self.log_widget.cursor_type = "row"

            # --- NEU: Container für Log-Ansicht mit Regex-Input ---
            log_view_container = Vertical(
                Input(
                    placeholder="Regex-Filter (z.B. 'fehler|warnung' oder '1.1.25') und Enter...", 
                    id="regex_filter_input"
                ),
                self.log_widget,
                id="log_view_container"
            )
            # --- ENDE NEU ---

            # --- NEU: Datei-Browser mit Pfad-Eingabe ---
            file_browser_container = Vertical(
                Input(placeholder="Pfad eingeben (z.B. C:/ oder //Server/Share) und Enter drücken...", id="path_changer"),
                FilteredDirectoryTree(".", id="file_browser"),
                id="files_container"
            )
            # ---

            TS_WIDTH = 24
            PA_WIDTH = 10
            GA_WIDTH = 10
            PAYLOAD_WIDTH = 25
            COLUMN_SEPARATORS_WIDTH = 6 
            fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
            available_width = self.app.size.width
            remaining_width = available_width - fixed_width - 4 
            name_width = max(10, remaining_width // 2)
            
            self.log_widget.add_column("Timestamp", key="ts", width=TS_WIDTH)
            self.log_widget.add_column("PA", key="pa", width=PA_WIDTH)
            self.log_widget.add_column("Gerät (PA)", key="pa_name", width=name_width)
            self.log_widget.add_column("GA", key="ga", width=GA_WIDTH)
            self.log_widget.add_column("Gruppenadresse (GA)", key="ga_name", width=name_width)
            self.log_widget.add_column("Payload", key="payload", width=PAYLOAD_WIDTH)

            tabs.add_pane(TabPane("Gebäudestruktur", building_tree, id="building_pane"))
            tabs.add_pane(TabPane("Physikalische Adressen", pa_tree, id="pa_pane"))
            tabs.add_pane(TabPane("Gruppenadressen", ga_tree, id="ga_pane"))
            
            # --- GEÄNDERT: Den Container statt nur das Widget hinzufügen ---
            tabs.add_pane(TabPane("Log-Ansicht", log_view_container, id="log_pane"))
            
            tabs.add_pane(TabPane("Dateien", file_browser_container, id="files_pane"))

            logging.debug("on_data_loaded: Populiere 'building_tree'...")
            self._populate_tree_from_data(building_tree, self.building_tree_data)
            logging.debug("on_data_loaded: Populiere 'pa_tree'...")
            self._populate_tree_from_data(pa_tree, self.pa_tree_data)
            logging.debug("on_data_loaded: Populiere 'ga_tree'...")
            self._populate_tree_from_data(ga_tree, self.ga_tree_data) 
            logging.debug("on_data_loaded: Bäume popoluiert. UI ist fast fertig.")

            tabs.disabled = False
            
            try:
                self.query_one(TabbedContent).focus()
            except Exception:
                pass
            
            self.call_later(self._load_log_file_and_update_views)
        except Exception as e:
            logging.error(f"on_data_loaded: Kritischer Fehler beim UI-Aufbau: {e}", exc_info=True) 
            self.show_startup_error(e, traceback.format_exc())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Wechselt das Verzeichnis des Datei-Browsers ODER setzt den Regex-Filter."""
        
        # Handler für Datei-Browser
        if event.input.id == "path_changer":
            # 1. Anführungszeichen entfernen
            raw_input = event.value.strip().strip('"').strip("'")
            
            if not raw_input: return

            target_path = raw_input
            
            try:
                # 2. Prüfen, ob der Pfad direkt gültig ist
                if not os.path.isdir(target_path):
                    # 3. Wenn nicht, versuchen aufzulösen
                    logging.debug(f"Pfad '{target_path}' nicht direkt gefunden, versuche resolve()")
                    target_path = str(Path(raw_input).resolve())

                # 4. Endgültige Prüfung
                if os.path.isdir(target_path):
                    self.query_one("#file_browser", DirectoryTree).path = target_path
                    self.notify(f"Verzeichnis gewechselt: {target_path}")
                else:
                    if os.name == 'nt' and target_path.startswith(r'\\') and len(target_path.split(os.sep)) == 3:
                         try:
                            self.query_one("#file_browser", DirectoryTree).path = target_path
                            self.notify(f"Server-Ansicht geöffnet: {target_path}")
                         except Exception as e:
                            self.notify(f"Fehler beim Laden von Server {target_path}: {e}", severity="error")
                    else:
                        self.notify(f"Verzeichnis nicht gefunden: {target_path}", severity="error")
            except Exception as e:
                self.notify(f"Pfad-Fehler: {e}", severity="error")

        # --- NEU: Handler für Regex-Filter ---
        elif event.input.id == "regex_filter_input":
            filter_text = event.value
            
            if not filter_text:
                self.regex_filter = None
                self.regex_filter_string = ""
                self.notify("Regex-Filter entfernt.")
            else:
                try:
                    # Case-insensitive
                    self.regex_filter = re.compile(filter_text, re.IGNORECASE)
                    self.regex_filter_string = filter_text
                    self.notify(f"Regex-Filter aktiv: '{filter_text}'")
                except re.error as e:
                    self.regex_filter = None
                    self.regex_filter_string = ""
                    self.notify(f"Ungültiger Regex: {e}", severity="error")
            
            # Filter-Ansicht neu aufbauen
            self._refilter_log_view()
        # --- ENDE NEU ---
    
    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Wird aufgerufen, wenn im Dateien-Tab eine Datei ausgewählt wird."""
        event.stop()
        file_path = str(event.path)
        
        if file_path.lower().endswith((".log", ".zip", ".txt")):
            self.notify(f"Lade Datei: {os.path.basename(file_path)}")
            self.config['log_file'] = file_path
            
            self._load_log_file_and_update_views()
            
            self.query_one(TabbedContent).active = "log_pane"

    def _populate_tree_from_data(self, tree: Tree, data: TreeData, expand_all: bool = False):
        logging.debug(f"_populate_tree_from_data: Starte für Baum '{tree.id or 'unbekannt'}'.")
        tree.clear()
        def natural_sort_key(item: Tuple[str, Any]):
            key_str = str(item[0])
            return [int(c) if c.isdigit() else c.lower() for c in re.split('([0-9]+)', key_str)]
        def add_nodes(parent_node: TreeNode, children_data: Dict[str, TreeData]):
            for _, node_data in sorted(children_data.items(), key=natural_sort_key):
                label = node_data.get("name")
                if not label: continue
                child_node = parent_node.add(label, data=node_data.get("data"))
                if node_children := node_data.get("children"):
                    add_nodes(child_node, node_children)
        
        if data and "children" in data:
            add_nodes(tree.root, data["children"])
        
        logging.debug(f"_populate_tree_from_data: '{tree.id or 'unbekannt'}' Knoten hinzugefügt. Starte rekursives Label-Update...")
        self._update_tree_labels_recursively(tree.root)
        logging.debug(f"_populate_tree_from_data: '{tree.id or 'unbekannt'}' Label-Update beendet.")
        
        tree.root.collapse_all()
        if expand_all:
            tree.root.expand_all()

    def _process_log_lines(self):
        """
        Filtert die in `self.cached_log_data` zwischengespeicherten,
        angereicherten Log-Einträge basierend auf `self.selected_gas`
        und füllt die `DataTable`.
        """
        if not self.log_widget: return
        
        try:
            # --- KORREKTUR: Scroll-Position VOR dem Löschen prüfen ---
            # Wir nutzen >= statt ==, um Rundungsfehler abzufangen.
            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            # ---
            
            self.log_widget.clear()
            has_selection = bool(self.selected_gas)
            
            # --- NEU: Regex-Status abrufen ---
            has_regex_filter = bool(self.regex_filter)
            # --- ENDE NEU ---
    
            if not self.cached_log_data:
                 self.log_widget.add_row("[yellow]Keine Log-Daten geladen oder Log-Datei ist leer.[/yellow]")
                 self.log_widget.caption = "Keine Log-Daten"
                 return
            
            start_time = time.time()
            log_caption = ""
            log_entries_to_process = self.cached_log_data
            
            if has_selection:
                sorted_gas = sorted(list(self.selected_gas))
                filter_info = f"Filtere Log für {len(sorted_gas)} GAs: {', '.join(sorted_gas)}"
                logging.info(f"Applizieren des Log-Ansicht-Filters für {len(sorted_gas)} GAs.")
                log_caption = f"Filter aktiv ({len(sorted_gas)} GAs)"
            
            # --- NEU: Regex-Caption hinzufügen ---
            if has_regex_filter:
                log_caption += f" | Regex aktiv ('{self.regex_filter_string}')"
            # --- ENDE NEU ---

            # --- GEÄNDERT: Caption-Logik ---
            if not has_selection and not has_regex_filter:
                logging.info("Keine Auswahl. Zeige alle Log-Einträge.")
                if len(self.cached_log_data) > MAX_LOG_LINES_NO_FILTER:
                    log_entries_to_process = self.cached_log_data[-MAX_LOG_LINES_NO_FILTER:]
                    log_caption = f"Alle Einträge (Letzte {MAX_LOG_LINES_NO_FILTER} von {len(self.cached_log_data)} angezeigt)"
                    logging.warning(f"Kein Filter aktiv. Zeige nur die letzten {MAX_LOG_LINES_NO_FILTER} von {len(self.cached_log_data)} Log-Einträgen.")
                else:
                    log_caption = f"Alle Einträge ({len(self.cached_log_data)})"
            # --- ENDE ÄNDERUNG ---

            found_count = 0
            rows_to_add = []
            
            # --- GEÄNDERT: Filter-Schleife (GA UND Regex) ---
            for i, log_entry in enumerate(log_entries_to_process):
                
                # 1. GA-Filter (wie bisher)
                if has_selection and log_entry["ga"] not in self.selected_gas:
                    continue 
                
                # 2. NEU: Regex-Filter
                if has_regex_filter:
                    # Baue einen durchsuchbaren String aus allen Feldern
                    search_string = (
                        f"{log_entry['timestamp']} "
                        f"{log_entry['pa']} "
                        f"{log_entry['pa_name']} "
                        f"{log_entry['ga']} "
                        f"{log_entry['ga_name']} "
                        f"{log_entry['payload']}"
                    )
                    if not self.regex_filter.search(search_string):
                        continue # Trifft nicht zu -> nächste Zeile

                # 3. Hinzufügen (wenn beide Filter passiert wurden)
                rows_to_add.append((
                    log_entry["timestamp"],
                    log_entry["pa"],
                    log_entry["pa_name"],
                    log_entry["ga"],
                    log_entry["ga_name"],
                    log_entry["payload"]
                ))
                found_count += 1
            # --- ENDE ÄNDERUNG ---
            
            self.log_widget.add_rows(rows_to_add)
            
            duration = time.time() - start_time
            logging.info(f"Log-Ansicht gefiltert. {found_count} Einträge in {duration:.4f}s gefunden.")
            self.log_widget.caption = f"{found_count} Einträge gefunden. ({duration:.2f}s) | {log_caption}"

            # --- KORREKTUR: Ggf. ans Ende scrollen ---
            if is_at_bottom:
                self.log_widget.scroll_end(animate=False, duration=0.0)
            # ---

        except Exception as e:
            logging.error(f"Schwerer Fehler in _process_log_lines: {e}", exc_info=True)
            if self.log_widget:
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]Fehler beim Verarbeiten der Log-Zeilen: {e}[/red]")


    def _load_log_file_and_update_views(self):
        """
        Liest die Log-Datei (vollständig) von der Festplatte, 
        setzt den Tailing-Status zurück und aktualisiert alle Ansichten.
        """
        if not self.log_widget: return
        log_widget = self.log_widget
        
        log_file_path = self.config.get("log_file") or os.path.join(self.config.get("log_path", "."), "knx_bus.log")

        # --- Tailing-Status zurücksetzen ---
        self.last_log_mtime = None
        self.last_log_position = 0
        # ---

        if not os.path.exists(log_file_path):
            log_widget.clear()
            log_widget.add_row(f"[red]FEHLER: Log-Datei nicht gefunden unter '{log_file_path}'[/red]")
            log_widget.add_row("[dim]Mit 'o' eine andere Datei öffnen.[/dim]")
            self.cached_log_data = []
            self.payload_history.clear()
            self.action_toggle_log_reload(force_off=True) # Timer stoppen
            return
        
        start_time = time.time()
        logging.info(f"Lese Log-Datei von Festplatte: '{log_file_path}'")
        
        try:
            is_zip = log_file_path.lower().endswith(".zip")
            
            lines = []
            if is_zip:
                with zipfile.ZipFile(log_file_path, 'r') as zf:
                    log_files_in_zip = [name for name in zf.namelist() if name.lower().endswith('.log')]
                    if not log_files_in_zip:
                        raise FileNotFoundError("Keine .log-Datei im ZIP-Archiv gefunden.")
                    with zf.open(log_files_in_zip[0]) as log_file:
                        lines = io.TextIOWrapper(log_file, encoding='utf-8').readlines()
            else:
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # --- NEU: Tailing-Status setzen ---
                    self.last_log_position = f.tell() # Position am Dateiende
                self.last_log_mtime = os.path.getmtime(log_file_path)
                # --- ENDE NEU ---
            
            logging.debug("Starte parse_and_cache_log_data...")
            self.payload_history, self.cached_log_data = parse_and_cache_log_data(
                lines, 
                self.project_data,
                self.time_filter_start,
                self.time_filter_end
            )
            logging.debug("Beende parse_and_cache_log_data.")
            
            logging.debug("Aktualisiere Baum-Labels...")
            for tree in self.query(Tree):
                if tree.id == "file_browser": continue
                self._update_tree_labels_recursively(tree.root)
            logging.debug("Baum-Labels aktualisiert.")
            
            logging.debug("Starte _process_log_lines...")
            self._process_log_lines()
            logging.debug("Beende _process_log_lines.")

            # --- NEU: Auto-Reload je nach Dateityp ---
            if is_zip:
                self.action_toggle_log_reload(force_off=True)
            else:
                self.action_toggle_log_reload(force_on=True)
            # --- ENDE NEU ---

            # Footer-Fix: Fokus verzögert setzen
            def set_focus_final():
                try:
                    self.query_one(TabbedContent).focus()
                except Exception:
                    pass
            
            self.call_later(set_focus_final)

            duration = time.time() - start_time
            logging.info(f"Log-Datei '{os.path.basename(log_file_path)}' in {duration:.2f}s gelesen und verarbeitet.")

        except Exception as e:
            log_widget.clear()
            log_widget.add_row(f"\n[red]Fehler beim Verarbeiten der Log-Datei: {e}[/red]")
            log_widget.add_row(f"[dim]{traceback.format_exc()}[/dim]")
            logging.error(f"Fehler beim Verarbeiten von '{log_file_path}': {e}", exc_info=True)
            self.cached_log_data = []
            self.payload_history.clear()
            
    def _refilter_log_view(self) -> None:
        if not self.log_widget: return
        logging.info("Log-Ansicht wird mit gecachten Daten neu gefiltert (synchron).")
        self._process_log_lines()

    def _get_descendant_gas(self, node: TreeNode) -> Set[str]:
        gas = set()
        if isinstance(node.data, dict) and "gas" in node.data:
            gas.update(node.data["gas"])
        for child in node.children:
            gas.update(self._get_descendant_gas(child))
        return gas

    def _update_tree_labels_recursively(self, node: TreeNode) -> None:
        display_label = ""
        # Sicherstellen, dass es Daten gibt und kein Dateibaum-Objekt ist
        if isinstance(node.data, dict) and "original_name" in node.data:
            original_name = node.data["original_name"]
            display_label = original_name
            
            node_gas = node.data.get("gas", set())
            if node_gas:
                combined_history = []
                for ga in node_gas:
                    if ga in self.payload_history:
                        combined_history.extend(self.payload_history[ga])
                
                if combined_history:
                    combined_history.sort(key=lambda x: x['timestamp'])
                    latest_payloads = [item['payload'] for item in combined_history[-3:]]
                    current_payload = latest_payloads[-1]
                    previous_payloads = latest_payloads[-2::-1]
                    
                    payload_str = f"[bold yellow]{current_payload}[/]"
                    if previous_payloads:
                        history_str = ", ".join(previous_payloads)
                        payload_str += f" [dim]({history_str})[/dim]"
                    
                    display_label = f"{original_name} -> {payload_str}"
        else:
            # Fallback für Knoten ohne Daten
            display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))

        prefix = "[ ] "
        
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            elif selected_descendant_gas: 
                prefix = "[-] "
            else: 
                prefix = "[ ] "

        node.set_label(prefix + display_label)

        for child in node.children:
            self._update_tree_labels_recursively(child)



    def action_toggle_selection(self) -> None:
        try:
            active_tree = self.query_one(TabbedContent).active_pane.query_one(Tree)
            # Verhindere Crash im Dateibaum
            if active_tree.id == "file_browser": return

            node = active_tree.cursor_node
            if not node: return
            descendant_gas = self._get_descendant_gas(node)
            if not descendant_gas: return
            
            node_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))
            if descendant_gas.issubset(self.selected_gas):
                logging.info(f"Auswahl ENTFERNT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) entfernt.")
                self.selected_gas.difference_update(descendant_gas)
            else:
                logging.info(f"Auswahl HINZUGEFÜGT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) hinzugefügt.")
                self.selected_gas.update(descendant_gas)
            
            for tree in self.query(Tree):
                if tree.id == "file_browser": continue
                self._update_tree_labels_recursively(tree.root)
            
            if self.query_one(TabbedContent).active == "log_pane":
                self._refilter_log_view()

        except Exception as e:
            logging.error(f"Fehler bei action_toggle_selection: {e}", exc_info=True)            
    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "log_pane":
            logging.info("Log-Ansicht-Tab wurde aktiviert. Wende aktuellen Filter neu an.")
            self._refilter_log_view()

    def action_copy_label(self) -> None:
        try:
            active_tree = self.query_one(TabbedContent).active_pane.query_one(Tree)
            node = active_tree.cursor_node
            if node and isinstance(node.data, dict) and "original_name" in node.data:
                self.notify(f"Kopiert: '{node.data['original_name']}'")
            elif node:
                label_text = str(node.label)
                clean_label = re.sub(r"^(\[[ *\-]] )+", "", label_text)
                clean_label = re.sub(r"\s*->\s*.*$", "", clean_label)
                self.notify(f"Kopiert: '{clean_label}'")
        except Exception:
            self.notify("Konnte nichts kopieren.", severity="error")

    def action_open_log_file(self) -> None:
        """Wechselt zum 'Dateien'-Tab."""
        self.query_one(TabbedContent).active = "files_pane"
        try:
            self.query_one("#file_browser").focus()
        except:
            pass

    def action_reload_log_file(self) -> None:
        logging.info("Log-Datei wird manuell von Festplatte neu geladen.")
        self._load_log_file_and_update_views()
    
    def action_toggle_log_reload(self, force_on: bool = False, force_off: bool = False) -> None:
        """Schaltet den Auto-Reload-Timer um (oder erzwingt ihn)."""
        
        # Fall 1: Stoppen erzwingen (z.B. bei ZIP-Datei oder Fehler)
        if force_off:
            if self.log_reload_timer:
                self.log_reload_timer.stop()
                self.log_reload_timer = None
                self.notify("Log Auto-Reload [bold red]AUS[/] (Archiv/Fehler).", title="Log Ansicht")
                logging.info("Auto-Reload gestoppt (force_off).")
            return

        # Fall 2: Starten erzwingen (z.B. nach Laden von .log-Datei)
        if force_on:
            if not self.log_reload_timer:
                self.log_reload_timer = self.set_interval(1, self._efficient_log_tail)
                self.notify("Log Auto-Reload [bold green]EIN[/] (Standard).", title="Log Ansicht")
                logging.info("Auto-Reload (effizient) für .log-Datei gestartet.")
            return

        # Fall 3: Manueller Toggle durch Benutzer
        if self.log_reload_timer:
            self.log_reload_timer.stop()
            self.log_reload_timer = None
            self.notify("Log Auto-Reload [bold red]AUS[/].", title="Log Ansicht")
            logging.info("Auto-Reload manuell deaktiviert.")
        else:
            log_file_path = self.config.get("log_file")
            if log_file_path and log_file_path.lower().endswith((".log", ".txt")):
                self.log_reload_timer = self.set_interval(1, self._efficient_log_tail)
                self.notify("Log Auto-Reload [bold green]EIN[/].", title="Log Ansicht")
                logging.info("Auto-Reload (effizient) manuell aktiviert.")
            else:
                self.notify("Auto-Reload nur für .log/.txt-Dateien verfügbar.", severity="warning")


    def _efficient_log_tail(self) -> None:
        """
        Prüft effizient auf Log-Änderungen ('tail -f'-Logik) 
        und parst nur neue Zeilen.
        """
        log_file_path = self.config.get("log_file")
        
        if not log_file_path or not log_file_path.lower().endswith((".log", ".txt")):
            self.action_toggle_log_reload(force_off=True)
            return

        try:
            current_mtime = os.path.getmtime(log_file_path)
            if current_mtime == self.last_log_mtime:
                return 
            
            logging.debug(f"Log-Änderung erkannt (mtime {current_mtime}), lese ab Position {self.last_log_position}.")
            self.last_log_mtime = current_mtime

            with open(log_file_path, 'r', encoding='utf-8') as f:
                f.seek(self.last_log_position)
                new_lines = f.readlines()
                self.last_log_position = f.tell()
            
            if not new_lines:
                logging.debug("Log-Änderung war ein 'touch', keine neuen Zeilen.")
                return
            
            # 1. Nur neue Zeilen parsen und zurückgeben lassen
            new_cached_items = append_new_log_lines(
                new_lines, 
                self.project_data,
                self.payload_history,
                self.cached_log_data,
                self.time_filter_start,
                self.time_filter_end
            )
            
            if not new_cached_items:
                logging.debug("Keine neuen Zeilen nach Filterung (z.B. Zeitfilter).")
                return

            logging.debug(f"{len(new_cached_items)} neue Zeilen verarbeitet. Aktualisiere Tabelle...")

            # 2. UI effizient aktualisieren
            has_selection = bool(self.selected_gas)
            
            # --- NEU: Regex-Status abrufen ---
            has_regex_filter = bool(self.regex_filter)
            # --- ENDE NEU ---
            
            rows_to_add = []

            # --- GEÄNDERT: Diese Schleife wendet jetzt BEIDE Filter an ---
            logging.debug(f"Filtere {len(new_cached_items)} neue Zeilen (GA: {has_selection}, Regex: {has_regex_filter})...")
            
            for item in new_cached_items:
                
                # 1. GA-Filter
                if has_selection and item["ga"] not in self.selected_gas:
                    continue
                
                # 2. Regex-Filter
                if has_regex_filter:
                    search_string = (
                        f"{item['timestamp']} "
                        f"{item['pa']} "
                        f"{item['pa_name']} "
                        f"{item['ga']} "
                        f"{item['ga_name']} "
                        f"{item['payload']}"
                    )
                    if not self.regex_filter.search(search_string):
                        continue

                # 3. Hinzufügen
                rows_to_add.append((
                    item["timestamp"], item["pa"], item["pa_name"],
                    item["ga"], item["ga_name"], item["payload"]
                ))
            
            logging.debug(f"{len(rows_to_add)} neue Zeilen passen zu den Filtern.")
            # --- ENDE ÄNDERUNG ---
            
            if not rows_to_add:
                return # Nichts zu zeichnen

            # --- KORREKTUR: Scroll-Position VOR dem Hinzufügen prüfen ---
            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            # ---

            # 3. Nur die neuen, gefilterten Zeilen ANHÄNGEN
            self.log_widget.add_rows(rows_to_add)
            
            # 4. Limit einhalten (nur wenn kein Filter aktiv ist)
            if not has_selection and not has_regex_filter:
                current_rows = self.log_widget.row_count
                if current_rows > MAX_LOG_LINES_NO_FILTER:
                    num_to_remove = current_rows - MAX_LOG_LINES_NO_FILTER
                    logging.debug(f"Entferne {num_to_remove} alte Zeilen von oben.")
                    self.log_widget.remove_row(0, num_to_remove) 
 
            # --- KORREKTUR: Ggf. ans Ende scrollen ---
            if is_at_bottom:
                self.log_widget.scroll_end(animate=False, duration=0.0)
            # ---

            # 5. UI-Bäume (Payloads) aktualisieren
            for tree in self.query(Tree):
                if tree.id == "file_browser": continue
                self._update_tree_labels_recursively(tree.root)
            
        except FileNotFoundError:
            self.notify(f"Log-Datei '{log_file_path}' nicht mehr gefunden.", severity="error")
            self.action_toggle_log_reload(force_off=True)
        except Exception as e:
            logging.error(f"Fehler im efficient_log_tail: {e}", exc_info=True)
            self.notify(f"Fehler beim Log-Reload: {e}", severity="error")
            self.action_toggle_log_reload(force_off=True)
            
    def action_time_filter(self) -> None:
        def parse_time_input(time_str: str) -> Optional[datetime_time]:
            if not time_str:
                return None
            try:
                return datetime.strptime(time_str, "%H:%M:%S").time()
            except ValueError:
                try:
                    return datetime.strptime(time_str, "%H:%M").time()
                except ValueError:
                    self.notify(f"Ungültiges Zeitformat: '{time_str}'. Bitte HH:MM oder HH:MM:SS verwenden.",
                                severity="error", timeout=5)
                    return None

        def handle_filter_result(result: Tuple[Optional[str], Optional[str]]):
            start_str, end_str = result
            
            if start_str is None and end_str is None:
                self.notify("Zeitfilterung abgebrochen.")
                return

            new_start = parse_time_input(start_str) if start_str else None
            new_end = parse_time_input(end_str) if end_str else None
            
            if (start_str and new_start is None) or \
               (end_str and new_end is None):
                return

            self.time_filter_start = new_start
            self.time_filter_end = new_end
            
            if self.time_filter_start or self.time_filter_end:
                start_log = self.time_filter_start.strftime('%H:%M:%S') if self.time_filter_start else "Anfang"
                end_log = self.time_filter_end.strftime('%H:%M:%S') if self.time_filter_end else "Ende"
                logging.info(f"Zeitfilter gesetzt: {start_log} -> {end_log}")
                self.notify(f"Zeitfilter aktiv: {start_log} -> {end_log}")
            else:
                logging.info("Zeitfilter entfernt.")
                self.notify("Zeitfilter entfernt.")
            
            self._load_log_file_and_update_views()

        start_val = self.time_filter_start.strftime('%H:%M:%S') if self.time_filter_start else ""
        end_val = self.time_filter_end.strftime('%H:%M:%S') if self.time_filter_end else ""
        
        self.push_screen(TimeFilterScreen(start_val, end_val), handle_filter_result)

    def _filter_tree_data(self, original_data: TreeData, filter_text: str) -> Tuple[Optional[TreeData], bool]:
        if not original_data: return None, False
        
        node_name_to_check = original_data.get("data", {}).get("original_name") or original_data.get("name", "")
        is_direct_match = filter_text in node_name_to_check.lower()

        if is_direct_match: return original_data.copy(), True

        if original_children := original_data.get("children"):
            filtered_children = {}
            has_matching_descendant = False
            for key, child_data in original_children.items():
                filtered_child_data, child_has_match = self._filter_tree_data(child_data, filter_text)
                if child_has_match and filtered_child_data:
                    has_matching_descendant = True
                    filtered_children[key] = child_data
            
            if has_matching_descendant:
                new_node_data = original_data.copy()
                new_node_data["children"] = filtered_children
                return new_node_data, True
        return None, False

    def action_reset_filter(self) -> None:
        try:
            tabs = self.query_one(TabbedContent)
            active_pane = tabs.active_pane
            tree = active_pane.query_one(Tree)
            
            original_data = None
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            
            if original_data:
                logging.info(f"Baumfilter für '{tabs.active}' wird zurückgesetzt.")
                self._populate_tree_from_data(tree, original_data)
                self.notify("Filter zurückgesetzt.")
            else:
                self.notify("Konnte Originaldaten für Reset nicht finden.", severity="warning")
        except Exception as e:
            logging.error(f"Fehler beim Zurücksetzen des Filters: {e}", exc_info=True)
            self.notify("Kein aktiver Baum zum Zurücksetzen gefunden.", severity="error")

    def action_filter_tree(self) -> None:
        try:
            tabs = self.query_one(TabbedContent)
            active_pane = tabs.active_pane
            tree = active_pane.query_one(Tree)
        except Exception:
            self.notify("Kein aktiver Baum zum Filtern gefunden.", severity="error")
            return

        def filter_callback(filter_text: str):
            if not filter_text:
                self.action_reset_filter()
                return
            
            lower_filter_text = filter_text.lower()
            self.notify(f"Filtere Baum mit: '{filter_text}'...")
            logging.info(f"Baumfilterung für Tab '{tabs.active}' gestartet mit Text: '{filter_text}'")
            start_time = time.time()

            original_data = None
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            
            if not original_data:
                self.notify("Keine Daten zum Filtern für diesen Tab gefunden.", severity="error")
                return

            filtered_data, has_matches = self._filter_tree_data(original_data, lower_filter_text)
            
            duration = time.time() - start_time
            logging.info(f"Baumfilterung abgeschlossen in {duration:.4f}s. Treffer gefunden: {has_matches}")

            if not has_matches:
                self.notify(f"Keine Treffer für '{filter_text}' gefunden.")
            
            self._populate_tree_from_data(tree, filtered_data or {}, expand_all=True)

    def on_resize(self, event: events.Resize) -> None:
        if not self.log_widget:
            return

        logging.debug(f"on_resize: Fenstergröße geändert auf {event.size.width}. Berechne Spalten neu.")

        TS_WIDTH = 24
        PA_WIDTH = 10
        GA_WIDTH = 10
        PAYLOAD_WIDTH = 25
        COLUMN_SEPARATORS_WIDTH = 6 
        fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
        
        available_width = event.size.width
        remaining_width = available_width - fixed_width - 4 # Puffer
        name_width = max(10, remaining_width // 2)

        try:
            self.log_widget.columns["pa_name"].width = name_width
            self.log_widget.columns["ga_name"].width = name_width
        except KeyError:
            logging.warning("on_resize: Spalten 'pa_name' oder 'ga_name' nicht gefunden zum Anpassen.")
    
### --- START ---
def main():
    try:
        logging.basicConfig(
            level=LOG_LEVEL, 
            filename='knx_lens.log', 
            filemode='w',
            format='%(asctime)s - %(levelname)s - %(message)s', 
            encoding='utf-8'
        )
        logging.info("Anwendung gestartet.")

        if not os.path.exists("knx-lens.css"):
            logging.warning("knx-lens.css nicht gefunden. Die App wird nicht korrekt dargestellt.")
            print("WARNUNG: 'knx-lens.css' nicht im selben Verzeichnis gefunden.", file=sys.stderr)
        
        load_dotenv()
        parser = argparse.ArgumentParser(description="KNX-Lens")
        parser.add_argument("--path", help="Pfad zur .knxproj Datei (überschreibt .env)")
        parser.add_argument("--log-file", help="Pfad zur Log-Datei für die Filterung (überschreibt .env)")
        parser.add_argument("--password", help="Passwort für die Projektdatei (überschreibt .env)")
        args = parser.parse_args()

        config = {
            'knxproj_path': args.path or os.getenv('KNX_PROJECT_PATH'),
            'log_file': args.log_file or os.getenv('LOG_FILE'),
            'password': args.password or os.getenv('KNX_PASSWORD'),
            'log_path': os.getenv('LOG_PATH')
        }

        if not config['knxproj_path']:
            logging.critical("Projektpfad nicht gefunden.")
            print("FEHLER: Projektpfad nicht gefunden. Bitte 'setup.py' ausführen oder mit --path angeben.", file=sys.stderr)
            sys.exit(1)
        
        app = KNXLens(config=config)
        app.run()

    except Exception:
        logging.critical("Unbehandelter Fehler in der main() Funktion", exc_info=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()