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

# --- NEUE ABHÄNGIGKEIT ---
try:
    import yaml
except ImportError:
    print("FEHLER: 'PyYAML' ist nicht installiert. Bitte installieren: pip install PyYAML", file=sys.stderr)
    sys.exit(1)
# ---

# Third-party libraries
from dotenv import load_dotenv, set_key, find_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
# --- ÄNDERUNG: Footer entfernt ---
from textual.widgets import Header, Tree, Static, TabbedContent, TabPane, DataTable, DirectoryTree, Input
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
from knx_log_utils import parse_and_cache_log_data, append_new_log_lines
from knx_tui_screens import FilterInputScreen, TimeFilterScreen, FilteredDirectoryTree

# --- NEU: Import der Logik-Klasse ---
from knx_tui_logic import KNXTuiLogic
# ---


### --- SETUP & KONSTANTEN ---
LOG_LEVEL = logging.INFO
TreeData = Dict[str, Any]

### --- TUI: HAUPTANWENDUNG ---
class KNXLens(App, KNXTuiLogic):
    CSS_PATH = "knx-lens.css"
    
    # --- FIX "KEINE FUNKTION": Alle Tasten müssen hier (mit show=True)
    # definiert sein, damit Textual die Aktionen registriert.
    # Unser manueller Footer in `update_footer` steuert die *Anzeige*.
    BINDINGS = [
        # Globale
        Binding("q", "quit", "Quit", show=True, priority=True),
        
        # Projekt-Baum Tasten
        Binding("a", "toggle_selection", "Auswahl", show=True),
        Binding("s", "save_filter", "Speichern", show=True),
        Binding("f", "filter_tree", "Filter", show=True),
        Binding("escape", "reset_filter", "Reset Filter", show=True),
        
        # Filter-Baum Tasten
        Binding("d", "delete_item", "Löschen", show=True),

        # Log-Ansicht Tasten
        Binding("r", "reload_log_file", "Reload", show=True),
        Binding("t", "toggle_log_reload", "Auto-Reload", show=True),
        Binding("i", "time_filter", "Interval", show=True),
    ]
    # --- ENDE FIX ---

    def __init__(self, config: Dict):
        super().__init__()
        self.config = config
        self.project_data: Optional[Dict] = None
        self.building_tree_data: TreeData = {}
        self.pa_tree_data: TreeData = {}
        self.ga_tree_data: TreeData = {}
        
        # ... (Filter-Status, etc. bleiben gleich) ...
        self.selected_gas: Set[str] = set()
        self.regex_filter: Optional[re.Pattern] = None
        self.regex_filter_string: str = ""
        self.named_filter_path: Path = Path(".") / "named_filters.yaml"
        self.named_filters: Dict[str, List[str]] = {}
        self.named_filters_rules: Dict[str, Dict[str, Any]] = {}
        self.active_named_filters: Set[str] = set()
        self.active_named_regex_rules: List[re.Pattern] = []
        self.log_widget: Optional[DataTable] = None
        self.log_reload_timer: Optional[Timer] = None
        self.payload_history: Dict[str, List[Dict[str, str]]] = {}
        self.cached_log_data: List[Dict[str, str]] = []
        self.time_filter_start: Optional[datetime_time] = None
        self.time_filter_end: Optional[datetime_time] = None
        self.last_user_activity: float = time.time()
        self.log_view_is_dirty: bool = True 
        self.last_log_mtime: Optional[float] = None
        self.last_log_position: int = 0
        self.paging_warning_shown: bool = False
        self.max_log_lines = int(self.config.get('max_log_lines', 10000))
        self.reload_interval = float(self.config.get('reload_interval', 5.0))

        # --- FIX "TRÄGHEIT":
        # Set, das die Bäume enthält, die noch ein Payload-Update benötigen.
        self.trees_need_payload_update = {"#pa_tree", "#ga_tree"}
        # --- ENDE FIX ---
        
        self.tab_bindings_display = {
            # --- FIX "DATEIEN ÜBERALL": "o" hier hinzugefügt ---
            "building_pane": [
                Binding("a", "toggle_selection", "Auswahl"),
                Binding("s", "save_filter", "Speichern"),
                Binding("f", "filter_tree", "Filter"),
                Binding("escape", "reset_filter", "Reset"),
                binding_o_dateien,
            ],
            "pa_pane": [
                Binding("a", "toggle_selection", "Auswahl"),
                Binding("s", "save_filter", "Speichern"),
                Binding("f", "filter_tree", "Filter"),
                Binding("escape", "reset_filter", "Reset"),
                binding_o_dateien,
            ],
            "ga_pane": [
                Binding("a", "toggle_selection", "Auswahl"),
                Binding("s", "save_filter", "Speichern"),
                Binding("f", "filter_tree", "Filter"),
                Binding("escape", "reset_filter", "Reset"),
                binding_o_dateien,
            ],
            "filter_pane": [
                Binding("a", "toggle_selection", "Aktivieren"),
                Binding("d", "delete_item", "Löschen"),
                binding_o_dateien,
            ],
            "log_pane": [
                Binding("r", "reload_log_file", "Reload"),
                Binding("t", "toggle_log_reload", "Auto-Reload"),
                Binding("i", "time_filter", "Interval"),
                binding_o_dateien,
            ],
            # --- FIX "DATEIEN ÜBERALL": "o" hier entfernt ---
            "files_pane": [] 
        }
        
        # Globale Tasten, die *immer* angezeigt werden
        self.global_bindings_display = [
            Binding("q", "quit", "Quit"),
            # --- FIX "DATEIEN ÜBERALL": "o" hier entfernt ---
        ]


    def compose(self) -> ComposeResult:
        yield Header(name="KNX Projekt-Explorer")
        yield Vertical(Static("Lade und verarbeite Projektdatei...", id="loading_label"), id="loading_container")
        yield TabbedContent(id="main_tabs", disabled=True)
        # --- KORREKTUR: Footer durch Static ersetzt ---
        yield Static("", id="manual_footer")

    def show_startup_error(self, exc: Exception, tb_str: str) -> None:
        try:
            loading_label = self.query_one("#loading_label")
            loading_label.update(f"[bold red]FEHLER BEIM LADEN[/]\n[yellow]Meldung:[/] {exc}\n\n[bold]Traceback:[/]\n{tb_str}")
        except Exception:
            logging.critical("Konnte UI-Fehler nicht anzeigen.", exc_info=True)

    def on_mount(self) -> None:
        # (Bleibt fast gleich)
        logging.debug("on_mount: Starte 'UI-First'-Laden...")
        start_time = time.time()
        
        try:
            # 1. Projekt laden
            proj_start = time.time()
            self.project_data = load_or_parse_project(self.config['knxproj_path'], self.config['password'])
            logging.debug(f"Projekt geladen in {time.time() - proj_start:.4f}s")
            
            knxproj_dir = Path(self.config['knxproj_path']).parent
            self.named_filter_path = knxproj_dir / "named_filters.yaml"
            logging.info(f"Named-Filter-Pfad: {self.named_filter_path}")
            
            # 2. Baum-Daten bauen
            tree_data_start = time.time()
            self.ga_tree_data = build_ga_tree_data(self.project_data)
            self.pa_tree_data = build_pa_tree_data(self.project_data)
            self.building_tree_data = build_building_tree_data(self.project_data)
            logging.debug(f"Baum-Daten gebaut in {time.time() - tree_data_start:.4f}s")

            # 3. UI Bauen
            ui_build_start = time.time()
            self.build_ui_tabs()
            logging.debug(f"UI-Tabs gebaut in {time.time() - ui_build_start:.4f}s")
            
            # 4. UI freigeben
            self.query_one("#loading_container").remove()
            tabs = self.query_one(TabbedContent)
            tabs.disabled = False
            tabs.focus()
            logging.info(f"UI-Start (Phase 1) abgeschlossen in {time.time() - start_time:.4f}s. App ist bedienbar.")

            # 5. Langsames Laden
            self.notify("Projekt geladen. Lade Logs im Hintergrund...")
            self.call_later(self.load_data_phase_2)
            
            # Initialen Footer setzen
            self.update_footer("building_pane")
            
        except Exception as e:
            self.show_startup_error(e, traceback.format_exc())
    
    def load_data_phase_2(self) -> None:
        logging.debug("load_data_phase_2: Starte Phase 2 (Log-Laden)...")
        start_time = time.time()
        
        try:
            # 1. Named Filters laden
            filter_load_start = time.time()
            self._load_named_filters()
            logging.debug(f"Named Filters geladen in {time.time() - filter_load_start:.4f}s")

            # 2. Logs laden
            log_load_start = time.time()
            self._load_log_file_data_only()
            logging.debug(f"Log-Daten geladen in {time.time() - log_load_start:.4f}s")

            # 3. Bäume popolieren
            populate_start = time.time()
            self._populate_tree_from_data(self.query_one("#building_tree", Tree), self.building_tree_data)
            self._populate_tree_from_data(self.query_one("#pa_tree", Tree), self.pa_tree_data)
            self._populate_tree_from_data(self.query_one("#ga_tree", Tree), self.ga_tree_data)
            self._populate_named_filter_tree()
            logging.debug(f"Bäume popoliert in {time.time() - populate_start:.4f}s")

            # 4. Baum-Labels aktualisieren
            labels_start = time.time()
            logging.debug("Aktualisiere Baum-Labels...")
            
            # --- FIX "TRÄGHEIT": Nur den ersten Baum beim Start aktualisieren ---
            logging.debug("Aktualisiere Labels für #building_tree (initial)...")
            self._update_tree_labels_recursively(self.query_one("#building_tree", Tree).root)
            logging.debug(f"Baum-Labels für #building_tree aktualisiert in {time.time() - labels_start:.4f}s")
            # Die anderen Bäume (#pa_tree, #ga_tree) werden in on_tabbed_content_tab_activated geladen
            # --- ENDE FIX ---

            # 5. Initiale Log-Ansicht rendern
            render_start = time.time()
            logging.debug("Starte _process_log_lines (initiale Log-Ansicht)...")
            self.log_view_is_dirty = True
            self._process_log_lines()
            self.log_view_is_dirty = False 
            logging.debug(f"_process_log_lines beendet in {time.time() - render_start:.4f}s")

            # 6. Auto-Reload starten
            if not (self.config.get("log_file") or "").lower().endswith(".zip"):
                 self.action_toggle_log_reload(force_on=True)
            
            # 7. Fokus auf ersten Tab setzen (löst on_tabbed_content_tab_activated aus)
            self.query_one(TabbedContent).active = "building_pane"
            
            logging.info(f"Phase 2 (Log-Laden & UI-Finish) abgeschlossen in {time.time() - start_time:.4f}s")

        except Exception as e:
            logging.error(f"Fehler in Phase 2 (load_data_phase_2): {e}", exc_info=True)
            self.notify(f"Fehler beim Laden der Log-Datei: {e}", severity="error")

    def build_ui_tabs(self) -> None:
        # (Bleibt gleich wie im Vordurchgang)
        logging.debug("build_ui_tabs: Beginne mit UI-Aufbau.")
        tabs = self.query_one(TabbedContent)
        
        building_tree = Tree("Gebäude", id="building_tree")
        pa_tree = Tree("Linien", id="pa_tree")
        ga_tree = Tree("Funktionen", id="ga_tree")
        filter_tree = Tree("Filter-Gruppen", id="named_filter_tree")
        named_filter_container = Vertical(filter_tree, id="named_filter_container")
        
        self.log_widget = DataTable(id="log_view")
        self.log_widget.cursor_type = "row"
        
        log_filter_input = Input(
            placeholder="Globaler AND-Regex-Filter (z.B. 'fehler|warnung')...", 
            id="regex_filter_input"
        )
        log_view_container = Vertical(log_filter_input, self.log_widget, id="log_view_container")
        
        path_changer_input = Input(
            placeholder="Pfad eingeben (z.B. C:/ oder //Server/Share) und Enter drücken...", 
            id="path_changer"
        )
        file_browser_tree = FilteredDirectoryTree(".", id="file_browser")
        file_browser_container = Vertical(path_changer_input, file_browser_tree, id="files_container")

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
        tabs.add_pane(TabPane("Filter-Gruppen", named_filter_container, id="filter_pane"))
        tabs.add_pane(TabPane("Log-Ansicht", log_view_container, id="log_pane"))
        tabs.add_pane(TabPane("Dateien", file_browser_container, id="files_pane"))
        
        logging.debug("build_ui_tabs: UI-Tabs erstellt.")
    
    def _reset_user_activity(self) -> None:
        logging.debug("User activity detected, resetting idle timer.")
        self.last_user_activity = time.time()
        if not self.log_reload_timer:
            log_file_path = self.config.get("log_file")
            if log_file_path and log_file_path.lower().endswith((".log", ".txt")):
                self.action_toggle_log_reload(force_on=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._reset_user_activity() 
        if event.input.id == "path_changer":
            raw_input = event.value.strip().strip('"').strip("'")
            if not raw_input: return
            target_path = raw_input
            try:
                if not os.path.isdir(target_path):
                    logging.debug(f"Pfad '{target_path}' nicht direkt gefunden, versuche resolve()")
                    target_path = str(Path(raw_input).resolve())
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
        elif event.input.id == "regex_filter_input":
            filter_text = event.value
            if not filter_text:
                self.regex_filter = None
                self.regex_filter_string = ""
                self.notify("Regex-Filter entfernt.")
            else:
                try:
                    self.regex_filter = re.compile(filter_text, re.IGNORECASE)
                    self.regex_filter_string = filter_text
                    self.notify(f"Globaler AND-Regex-Filter aktiv: '{filter_text}'")
                except re.error as e:
                    self.regex_filter = None
                    self.regex_filter_string = ""
                    self.notify(f"Ungültiger Regex: {e}", severity="error")
            self.paging_warning_shown = False
            self.log_view_is_dirty = True
            self._refilter_log_view()
    
    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._reset_user_activity() 
        event.stop()
        file_path = str(event.path)
        if file_path.lower().endswith((".log", ".zip", ".txt")):
            self.notify(f"Lade Datei: {os.path.basename(file_path)}")
            self.config['log_file'] = file_path
            self._reload_log_file_sync()
            self.query_one(TabbedContent).activate_tab("log_pane")

    def action_toggle_selection(self) -> None:
        self._reset_user_activity() 
        try:
            # --- NEUER ANSATZ: Das fokussierte Widget holen ---
            focused_widget = self.app.focused
            
            tree = None
            
            # Prüfen, ob das fokussierte Widget ein Baum ist (und nicht der file_browser)
            if isinstance(focused_widget, Tree) and focused_widget.id != "file_browser":
                tree = focused_widget
            else:
                # Fallback: Versuche, den Baum im aktiven Tab zu finden
                # (falls der Fokus z.B. auf dem Tab-Header lag)
                try:
                    active_pane = self.query_one(TabbedContent).active_pane
                    tree = active_pane.query_one("Tree:not(#file_browser)")
                except Exception:
                    logging.warning("Aktion 'toggle_selection' konnte keinen fokussierten oder aktiven Baum finden.")
                    return

            if not tree:
                logging.warning("Aktion 'toggle_selection' hat keinen gültigen Baum gefunden.")
                return

            # --- Ab hier ist der Code identisch zum Original ---
            node = tree.cursor_node
            if not node: return
            if tree.id == "named_filter_tree":
                if not node.data: return
                filter_name = ""
                if isinstance(node.data, tuple):
                    filter_name = node.data[0]
                else:
                    filter_name = str(node.data)
                rules = self.named_filters_rules.get(filter_name)
                if not rules: return
                if filter_name in self.active_named_filters:
                    logging.info(f"Named Filter DEAKTIVIERT: '{filter_name}'")
                    self.active_named_filters.remove(filter_name)
                    self.selected_gas.difference_update(rules["gas"])
                else:
                    logging.info(f"Named Filter AKTIVIERT: '{filter_name}'")
                    self.active_named_filters.add(filter_name)
                    self.selected_gas.update(rules["gas"])
                self._rebuild_active_regexes()
                self._update_all_tree_prefixes()
            elif tree.id != "file_browser":
                descendant_gas = self._get_descendant_gas(node)
                if not descendant_gas and (not node.parent or node.parent.id == "#tree-root"):
                    for child in node.children:
                        descendant_gas.update(self._get_descendant_gas(child))
                elif not descendant_gas:
                     return
                node_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))
                if descendant_gas.issubset(self.selected_gas):
                    logging.info(f"Auswahl ENTFERNT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) entfernt.")
                    self.selected_gas.difference_update(descendant_gas)
                else:
                    logging.info(f"Auswahl HINZUGEFÜGT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) hinzugefügt.")
                    self.selected_gas.update(descendant_gas)
                logging.debug(f"Aktualisiere Präfixe AB '{node.label}'...")
                self._update_node_and_children_prefixes(node)
                if node.parent:
                    self._update_parent_prefixes_recursive(node.parent)
                self._update_named_filter_prefixes()
                logging.debug(f"Präfix-Update beendet.")
            self.paging_warning_shown = False 
            self.log_view_is_dirty = True
            if self.query_one(TabbedContent).active == "log_pane":
                self._refilter_log_view()
        except Exception as e:
            logging.error(f"Fehler bei action_toggle_selection: {e}", exc_info=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Aktualisiert den Footer und setzt den Fokus, wenn der Tab gewechselt wird."""
        self._reset_user_activity() 
        
        pane_id = event.pane.id
        logging.debug(f"Tab aktiviert: {pane_id}")

        # 1. Footer-Text manuell aktualisieren
        self.update_footer(pane_id)
        
        # --- FIX "TRÄGHEIT": Lade Baum-Payloads bei Bedarf nach ---
        tree_id = f"#{pane_id.replace('_pane', '_tree')}" # z.B. "pa_pane" -> "#pa_tree"
        if tree_id in self.trees_need_payload_update:
            try:
                self.notify(f"Lade Payloads für Baum '{tree_id}'...")
                logging.info(f"Aktualisiere Labels (mit Payloads) für {tree_id}...")
                start_time = time.time()
                
                self._update_tree_labels_recursively(self.query_one(tree_id, Tree).root)
                
                duration = time.time() - start_time
                logging.info(f"Labels für {tree_id} in {duration:.4f}s aktualisiert.")
                self.trees_need_payload_update.remove(tree_id)
            except Exception as e:
                logging.error(f"Fehler beim Nachladen der Labels für {tree_id}: {e}")
        # --- ENDE FIX ---

        # 2. Fokus für Tastatureingaben setzen
        try:
            if pane_id in ("building_pane", "pa_pane", "ga_pane", "filter_pane"):
                event.pane.query_one(Tree).focus()
            elif pane_id == "log_pane":
                self.query_one("#regex_filter_input", Input).focus()
            elif pane_id == "files_pane":
                self.query_one("#path_changer", Input).focus()
        except Exception as e:
            logging.warning(f"Fehler beim Setzen des Fokus für Tab {pane_id}: {e}")

        # 3. Log-Ansicht-Filterung (wie gehabt)
        if event.pane.id == "log_pane" and self.log_view_is_dirty:
            logging.info("Log-Ansicht-Tab wurde aktiviert und ist 'dirty'. Wende Filter neu an.")
            self._refilter_log_view()
        elif event.pane.id == "log_pane":
             logging.debug("Log-Ansicht-Tab aktiviert, aber 'clean'. Tue nichts.")

    def update_footer(self, pane_id: str) -> None:
        """
        Aktualisiert das Static-Widget (manual_footer) manuell.
        """
        try:
            footer_static = self.query_one("#manual_footer", Static)
            
            # Globale Tasten holen
            global_bindings = self.global_bindings_display
            
            # Kontext-Tasten holen
            context_bindings = self.tab_bindings_display.get(pane_id, [])
            
            all_bindings = global_bindings + context_bindings
            
            # Baue den Text-String
            footer_text = "  ".join(
                f"[bold]{b.key.upper()}[/]:{b.description}" 
                for b in all_bindings
            )
            
            # Setze den Text im Static-Widget
            footer_static.update(footer_text)
            
        except Exception as e:
            logging.error(f"Fehler beim Aktualisieren des Footers: {e}", exc_info=True)
            try:
                footer_static.update("[bold]Q[/]:Quit")
            except:
                pass 

    def action_open_log_file(self) -> None:
        """Wechselt zum 'Dateien'-Tab."""
        self._reset_user_activity() 
        try:
            self.query_one(TabbedContent).activate_tab("files_pane")
        except Exception as e:
            logging.error(f"Konnte Tab 'files_pane' nicht aktivieren: {e}")
            self.notify(f"Fehler beim Öffnen des Datei-Tabs: {e}", severity="error")

    # ... (Alle anderen Aktionen: reload, save, delete, toggle_reload, time_filter, filter_tree bleiben gleich) ...
    def action_reload_log_file(self) -> None:
        logging.info("Log-Datei wird manuell von Festplatte neu geladen.")
        self._reload_log_file_sync()
    
    def action_save_filter(self) -> None:
        if not self.selected_gas:
            self.notify("Keine GAs ausgewählt, nichts zu speichern.", severity="warning")
            return
        def save_callback(name: str):
            if not name:
                self.notify("Speichern abgebrochen.", severity="warning")
                return
            new_rules = sorted(list(self.selected_gas))
            self.named_filters[name] = new_rules
            self._save_named_filters()
            self._load_named_filters()
            self._populate_named_filter_tree()
            self.notify(f"Filter '{name}' mit {len(new_rules)} GAs gespeichert.")
        self.push_screen(FilterInputScreen(prompt="Aktuelle Auswahl speichern unter:"), save_callback)

    def action_delete_item(self) -> None:
        try:
            active_pane = self.query_one(TabbedContent).active_pane
            if active_pane.id != "filter_pane":
                self.notify("Löschen ('d') ist nur im Tab 'Filter-Gruppen' aktiv.", severity="info")
                return
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not node.data:
                self.notify("Kein Filter zum Löschen ausgewählt.", severity="warning")
                return
            if isinstance(node.data, tuple):
                filter_name, rule_str = node.data
                def confirm_rule_delete(confirm: str):
                    if confirm.lower() in ["ja", "j", "yes", "y"]:
                        try:
                            self.named_filters[filter_name].remove(rule_str)
                            self._save_named_filters()
                            self._load_named_filters()
                            self._populate_named_filter_tree()
                            self.notify(f"Regel '{rule_str}' aus '{filter_name}' gelöscht.")
                        except Exception as e:
                            self.notify(f"Fehler beim Löschen der Regel: {e}", severity="error")
                    else:
                        self.notify("Löschen abgebrochen.")
                self.push_screen(FilterInputScreen(prompt=f"Regel '{rule_str}' wirklich löschen? (Ja/Nein)"), confirm_rule_delete)
            elif isinstance(node.data, str):
                filter_name = str(node.data)
                def confirm_filter_delete(confirm: str):
                    if confirm.lower() in ["ja", "j", "yes", "y"]:
                        try:
                            del self.named_filters[filter_name]
                            if filter_name in self.named_filters_rules:
                                del self.named_filters_rules[filter_name]
                            if filter_name in self.active_named_filters:
                                self.active_named_filters.remove(filter_name)
                            self._save_named_filters()
                            self._populate_named_filter_tree()
                            self._rebuild_active_regexes()
                            self._update_all_tree_prefixes()
                            self.log_view_is_dirty = True
                            self._refilter_log_view()
                            self.notify(f"Filter '{filter_name}' gelöscht.")
                        except Exception as e:
                            self.notify(f"Fehler beim Löschen: {e}", severity="error")
                    else:
                        self.notify("Löschen abgebrochen.")
                self.push_screen(FilterInputScreen(prompt=f"Filter '{filter_name}' wirklich löschen? (Ja/Nein)"), confirm_filter_delete)
        except Exception as e:
            logging.error(f"Fehler bei action_delete_item: {e}", exc_info=True)
    
    def action_toggle_log_reload(self, force_on: bool = False, force_off: bool = False) -> None:
        TIMER_INTERVAL = self.reload_interval 
        if force_off:
            if self.log_reload_timer:
                self.log_reload_timer.stop()
                self.log_reload_timer = None
                if not force_off: 
                    self.notify("Log Auto-Reload [bold red]AUS[/] (Archiv/Fehler).", title="Log Ansicht")
                logging.info("Auto-Reload gestoppt (force_off).")
            return
        if force_on:
            self.last_user_activity = time.time() 
            if not self.log_reload_timer:
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]EIN[/] ({TIMER_INTERVAL}s).", title="Log Ansicht")
                logging.info(f"Auto-Reload (effizient) für .log-Datei gestartet (Intervall: {TIMER_INTERVAL}s).")
            return
        self._reset_user_activity() 
        if self.log_reload_timer:
            self.log_reload_timer.stop()
            self.log_reload_timer = None
            self.notify("Log Auto-Reload [bold red]AUS[/].", title="Log Ansicht")
            logging.info("Auto-Reload manuell deaktiviert.")
        else:
            log_file_path = self.config.get("log_file")
            if log_file_path and log_file_path.lower().endswith((".log", ".txt")):
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]EIN[/] ({TIMER_INTERVAL}s).", title="Log Ansicht")
                logging.info(f"Auto-Reload (effizient) manuell aktiviert (Intervall: {TIMER_INTERVAL}s).")
            else:
                self.notify("Auto-Reload nur für .log/.txt-Dateien verfügbar.", severity="warning")
            
    def action_time_filter(self) -> None:
        self._reset_user_activity() 
        def parse_time_input(time_str: str) -> Optional[datetime_time]:
            if not time_str: return None
            try: return datetime.strptime(time_str, "%H:%M:%S").time()
            except ValueError:
                try: return datetime.strptime(time_str, "%H:%M").time()
                except ValueError:
                    self.notify(f"Ungültiges Zeitformat: '{time_str}'. Bitte HH:MM oder HH:MM:SS verwenden.", severity="error", timeout=5)
                    return None
        def handle_filter_result(result: Tuple[Optional[str], Optional[str]]):
            start_str, end_str = result
            if start_str is None and end_str is None:
                self.notify("Zeitfilterung abgebrochen.")
                return
            new_start = parse_time_input(start_str) if start_str else None
            new_end = parse_time_input(end_str) if end_str else None
            if (start_str and new_start is None) or (end_str and new_end is None): return
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
            self.paging_warning_shown = False
            self.log_view_is_dirty = True
            self._reload_log_file_sync()
        start_val = self.time_filter_start.strftime('%H:%M:%S') if self.time_filter_start else ""
        end_val = self.time_filter_end.strftime('%H:%M:%S') if self.time_filter_end else ""
        self.push_screen(TimeFilterScreen(start_val, end_val), handle_filter_result)

    def action_filter_tree(self) -> None:
        self._reset_user_activity() 
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
                if tabs.active == "filter_pane":
                    self.notify("Filtern für Named Filters noch nicht implementiert.", severity="warning")
                    return
                self.notify("Keine Daten zum Filtern für diesen Tab gefunden.", severity="error")
                return
            filtered_data, has_matches = self._filter_tree_data(original_data, lower_filter_text)
            duration = time.time() - start_time
            logging.info(f"Baumfilterung abgeschlossen in {duration:.4f}s. Treffer gefunden: {has_matches}")
            if not has_matches:
                self.notify(f"Keine Treffer für '{filter_text}' gefunden.")
            self._populate_tree_from_data(tree, filtered_data or {}, expand_all=True)


    def action_reset_filter(self) -> None:
        """Setzt den aktuell aktiven Baum auf den ungefilterten Zustand zurück."""
        self._reset_user_activity()
        try:
            tabs = self.query_one(TabbedContent)
            active_pane = tabs.active_pane
            tree = active_pane.query_one(Tree)
            
            original_data = None
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            elif tabs.active == "filter_pane":
                self._populate_named_filter_tree()
                self.notify("Filter-Baum neu geladen.")
                return
            elif tabs.active == "files_pane":
                return # Keine Aktion für den Dateibaum
                
            if original_data:
                self._populate_tree_from_data(tree, original_data, expand_all=False)
                
                # WICHTIG: Nach dem Neuladen des Baums gehen Payloads verloren.
                # Wir müssen ein Payload-Update für diesen Baum erzwingen.
                tree_id = f"#{tree.id}"
                if tree_id not in self.trees_need_payload_update:
                    self.trees_need_payload_update.add(tree_id)
                    # Manuell die Ladefunktion aufrufen, da der Tab bereits aktiv ist
                    self.call_later(self._trigger_payload_update_for_active_tab)

                self.notify("Baumfilter zurückgesetzt.")
                logging.info(f"Baumfilter für {tabs.active} zurückgesetzt.")
            
        except Exception as e:
            logging.error(f"Fehler bei action_reset_filter: {e}", exc_info=True)
            self.notify("Fehler beim Zurücksetzen des Filters.", severity="error")
            
    def action_reset_filter(self) -> None:
        """Setzt den aktuell aktiven Baum auf den ungefilterten Zustand zurück."""
        self._reset_user_activity()
        try:
            tabs = self.query_one(TabbedContent)
            active_pane = tabs.active_pane
            tree = active_pane.query_one(Tree)
            
            original_data = None
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            elif tabs.active == "filter_pane":
                self._populate_named_filter_tree()
                self.notify("Filter-Baum neu geladen.")
                return
            elif tabs.active == "files_pane":
                return # Keine Aktion für den Dateibaum
                
            if original_data:
                self._populate_tree_from_data(tree, original_data, expand_all=False)
                
                # WICHTIG: Nach dem Neuladen des Baums gehen Payloads verloren.
                # Wir müssen ein Payload-Update für diesen Baum erzwingen.
                tree_id = f"#{tree.id}"
                if tree_id not in self.trees_need_payload_update:
                    self.trees_need_payload_update.add(tree_id)
                    # Manuell die Ladefunktion aufrufen, da der Tab bereits aktiv ist
                    self.call_later(self._trigger_payload_update_for_active_tab)

                self.notify("Baumfilter zurückgesetzt.")
                logging.info(f"Baumfilter für {tabs.active} zurückgesetzt.")
            
        except Exception as e:
            logging.error(f"Fehler bei action_reset_filter: {e}", exc_info=True)
            self.notify("Fehler beim Zurücksetzen des Filters.", severity="error")

    def _trigger_payload_update_for_active_tab(self):
        """Hilfsfunktion, um das Payload-Update nach dem Reset auszulösen."""
        try:
            active_tab_id = self.query_one(TabbedContent).active
            active_pane = self.query_one(f"#{active_tab_id}")
            # Simuliere das erneute Aktivieren des Tabs, um das Laden auszulösen
            self.on_tabbed_content_tab_activated(
                TabbedContent.TabActivated(
                    self.query_one(TabbedContent), 
                    active_pane
                )
            )
        except Exception as e:
             logging.error(f"Fehler beim Triggern des Payload-Updates: {e}")

    def on_resize(self, event: events.Resize) -> None:
        # (Bleibt gleich)
        if not self.log_widget: return
        logging.debug(f"on_resize: Fenstergröße geändert auf {event.size.width}. Berechne Spalten neu.")
        TS_WIDTH = 24
        PA_WIDTH = 10
        GA_WIDTH = 10
        PAYLOAD_WIDTH = 25
        COLUMN_SEPARATORS_WIDTH = 6 
        fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
        available_width = event.size.width
        remaining_width = available_width - fixed_width - 4
        name_width = max(10, remaining_width // 2)
        try:
            self.log_widget.columns["pa_name"].width = name_width
            self.log_widget.columns["ga_name"].width = name_width
        except KeyError:
            logging.warning("on_resize: Spalten 'pa_name' oder 'ga_name' nicht gefunden zum Anpassen.")


    def _trigger_payload_update_for_active_tab(self):
        """Hilfsfunktion, um das Payload-Update nach dem Reset auszulösen."""
        try:
            active_tab_id = self.query_one(TabbedContent).active
            active_pane = self.query_one(f"#{active_tab_id}")
            # Simuliere das erneute Aktivieren des Tabs, um das Laden auszulösen
            self.on_tabbed_content_tab_activated(
                TabbedContent.TabActivated(
                    self.query_one(TabbedContent), 
                    active_pane
                )
            )
        except Exception as e:
             logging.error(f"Fehler beim Triggern des Payload-Updates: {e}")


    def on_resize(self, event: events.Resize) -> None:
        # (Bleibt gleich)
        if not self.log_widget: return
        logging.debug(f"on_resize: Fenstergröße geändert auf {event.size.width}. Berechne Spalten neu.")
        TS_WIDTH = 24
        PA_WIDTH = 10
        GA_WIDTH = 10
        PAYLOAD_WIDTH = 25
        COLUMN_SEPARATORS_WIDTH = 6 
        fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
        available_width = event.size.width
        remaining_width = available_width - fixed_width - 4
        name_width = max(10, remaining_width // 2)
        try:
            self.log_widget.columns["pa_name"].width = name_width
            self.log_widget.columns["ga_name"].width = name_width
        except KeyError:
            logging.warning("on_resize: Spalten 'pa_name' oder 'ga_name' nicht gefunden zum Anpassen.")
    
### --- START ---
def main():
    # (Bleibt gleich)
    try:
        logging.basicConfig(
            level=LOG_LEVEL, 
            filename='knx_lens.log', 
            filemode='w',
            format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
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
            'log_path': os.getenv('LOG_PATH'),
            'max_log_lines': os.getenv('MAX_LOG_LINES', '10000'),
            'reload_interval': os.getenv('RELOAD_INTERVAL', '5.0')
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