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

try:
    import yaml
except ImportError:
    print("FEHLER: 'PyYAML' ist nicht installiert. Bitte installieren: pip install PyYAML", file=sys.stderr)
    sys.exit(1)

# Third-party libraries
from dotenv import load_dotenv, set_key, find_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Header, Tree, Static, TabbedContent, TabPane, DataTable, DirectoryTree, Input
from textual.widgets.tree import TreeNode
from textual import events
from textual.timer import Timer

# Lokale Imports
from knx_project_utils import (
    load_or_parse_project, 
    build_ga_tree_data, 
    build_pa_tree_data, 
    build_building_tree_data
)
from knx_log_utils import parse_and_cache_log_data, append_new_log_lines
from knx_tui_screens import FilterInputScreen, TimeFilterScreen, FilteredDirectoryTree

from knx_tui_logic import KNXTuiLogic


### --- SETUP & KONSTANTEN ---
LOG_LEVEL = logging.INFO
TreeData = Dict[str, Any]

# Binding-Definitionen
binding_i_time_filter = Binding("i", "time_filter", "Time Filter", show=False)
binding_enter_load_file = Binding("enter", "load_file", "Load File", show=False)
binding_l_reload_filters = Binding("l", "reload_filter_tree", "Reload Groups", show=False)
binding_c_clear_selection = Binding("c", "clear_selection", "Clear Selection", show=False)
binding_n_new_rule = Binding("n", "new_rule", "New Rule", show=False)
binding_e_edit_rule = Binding("e", "edit_rule", "Edit Rule", show=False)
binding_ctrl_n_new_group = Binding("ctrl+n", "new_filter_group", "New Group", show=False)


### --- TUI: HAUPTANWENDUNG ---
class KNXLens(App, KNXTuiLogic):
    CSS_PATH = "knx-lens.css"
    
    BINDINGS = [
        # Globale
        Binding("q", "quit", "Quit", show=True, priority=True),
        
        # Projekt-Baum Tasten
        Binding("a", "toggle_selection", "Select", show=False),
        Binding("s", "save_filter", "Save Selection", show=False),
        Binding("f", "filter_tree", "Filter Tree", show=False),
        Binding("escape", "reset_filter", "Reset Filter", show=False),
        
        # Filter-Baum Tasten
        Binding("d", "delete_item", "Delete", show=False),

        # Log-Ansicht Tasten
        Binding("r", "reload_log_file", "Reload", show=False),
        Binding("t", "toggle_log_reload", "Auto-Reload", show=False),
        
        binding_i_time_filter,
        binding_enter_load_file,
        binding_l_reload_filters,
        binding_c_clear_selection,
        binding_n_new_rule,
        binding_e_edit_rule,
        binding_ctrl_n_new_group,
    ]

    def __init__(self, config: Dict):
        super().__init__()
        self.config = config
        self.project_data: Optional[Dict] = None
        self.building_tree_data: TreeData = {}
        self.pa_tree_data: TreeData = {}
        self.ga_tree_data: TreeData = {}
        
        self.named_filters_tree_data: TreeData = {}
        
        self.selected_gas: Set[str] = set()
        self.regex_filter: Optional[re.Pattern] = None
        self.regex_filter_string: str = ""
        self.named_filter_path: Path = Path(".") / "named_filters.yaml"
        self.named_filters: Dict[str, List[str]] = {}
        self.named_filters_rules: Dict[str, Dict[str, Any]] = {}
        self.active_named_filters: Set[str] = set()
        self.active_named_regex_rules: List[re.Pattern] = []
        self.log_widget: Optional[DataTable] = None
        self.log_caption_label: Optional[Static] = None
        self.log_auto_reload_enabled: bool = False         
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

        # Set, das die Bäume enthält, die noch ein Payload-Update benötigen.
        self.trees_need_payload_update = {"#pa_tree", "#ga_tree"}
        
        self.tab_bindings_display = {
            "building_pane": [
                Binding("a", "toggle_selection", "Select"),
                Binding("c", "clear_selection", "Clear Selection"),
                Binding("s", "save_filter", "Save Selection"),
                Binding("f", "filter_tree", "Filter Tree"),
                Binding("escape", "reset_filter", "Reset Filter"),
                binding_i_time_filter,
            ],
            "pa_pane": [
                Binding("a", "toggle_selection", "Select"),
                Binding("c", "clear_selection", "Clear Selection"),
                Binding("s", "save_filter", "Save Selection"),
                Binding("f", "filter_tree", "Filter Tree"),
                Binding("escape", "reset_filter", "Reset Filter"),
                binding_i_time_filter,
            ],
            "ga_pane": [
                Binding("a", "toggle_selection", "Select"),
                Binding("c", "clear_selection", "Clear Selection"),
                Binding("s", "save_filter", "Save Selection"),
                Binding("f", "filter_tree", "Filter Tree"),
                Binding("escape", "reset_filter", "Reset Filter"),
                binding_i_time_filter,
            ],
            "filter_pane": [
                Binding("a", "toggle_selection", "Activate"),
                Binding("ctrl+n", "new_filter_group", "New Group"),
                Binding("n", "new_rule", "New Rule"),
                Binding("e", "edit_rule", "Edit Rule"),
                Binding("d", "delete_item", "Delete"),
                Binding("l", "reload_filter_tree", "Reload Groups"),
                Binding("f", "filter_tree", "Filter Tree"),
                Binding("escape", "reset_filter", "Reset Filter"),
                Binding("c", "clear_selection", "Clear Selection"),
                binding_i_time_filter,
            ],
            "log_pane": [
                Binding("r", "reload_log_file", "Reload"),
                Binding("t", "toggle_log_reload", "Auto-Reload"),
                binding_i_time_filter,
            ],
            "files_pane": [
                binding_enter_load_file,
                binding_i_time_filter,
            ] 
        }
        
        # Globale Tasten, die *immer* angezeigt werden
        self.global_bindings_display = [
            Binding("q", "quit", "Quit"),
        ]


    def compose(self) -> ComposeResult:
        yield Header(name="KNX Project Explorer")
        yield Vertical(Static("Loading and processing project file...", id="loading_label"), id="loading_container")
        yield TabbedContent(id="main_tabs", disabled=True)
        yield Static("", id="manual_footer")

    def show_startup_error(self, exc: Exception, tb_str: str) -> None:
        try:
            loading_label = self.query_one("#loading_label")
            loading_label.update(f"[bold red]ERROR LOADING[/]\n[yellow]Message:[/] {exc}\n\n[bold]Traceback:[/]\n{tb_str}")
        except Exception:
            logging.critical("Konnte UI-Fehler nicht anzeigen.", exc_info=True)

    def on_mount(self) -> None:
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
            self.notify("Project loaded. Loading logs in the background...")
            self.call_later(self.load_data_phase_2)
            
            # Initialen Footer setzen
            self.update_footer("building_pane")
            
            # --- HIER IST DIE KORREKTUR FÜR DEN FOOTER ---
            self.query_one("#manual_footer", Static).styles.dock = "bottom"
            
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
            
            # Nur den ersten Baum beim Start aktualisieren
            logging.debug("Aktualisiere Labels für #building_tree (initial)...")
            self._update_tree_labels_recursively(self.query_one("#building_tree", Tree).root)
            logging.debug(f"Baum-Labels für #building_tree aktualisiert in {time.time() - labels_start:.4f}s")
            # Die anderen Bäume (#pa_tree, #ga_tree) werden in on_tabbed_content_tab_activated geladen

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
            self.notify(f"Error loading log file: {e}", severity="error")

    def build_ui_tabs(self) -> None:
        logging.debug("build_ui_tabs: Beginne mit UI-Aufbau.")
        tabs = self.query_one(TabbedContent)
        
        building_tree = Tree("Building", id="building_tree")
        pa_tree = Tree("Topology", id="pa_tree")
        ga_tree = Tree("Functions", id="ga_tree")
        filter_tree = Tree("Selection Groups", id="named_filter_tree")
        named_filter_container = Vertical(filter_tree, id="named_filter_container")
        
        self.log_widget = DataTable(id="log_view")
        self.log_widget.cursor_type = "row"
        
        log_filter_input = Input(
            placeholder="Global AND regex filter (e.g. 'error|warning')...", 
            id="regex_filter_input"
        )
        

        self.log_caption_label = Static("", id="log_caption")
        log_filter_input.styles.dock = "top"
        self.log_caption_label.styles.dock = "bottom"
        self.log_caption_label.styles.height = 1
        self.log_widget.styles.height = "1fr" 

        log_view_container = Vertical(
            log_filter_input, 
            self.log_widget, 
            self.log_caption_label, 
            id="log_view_container"
        )
        
        path_changer_input = Input(
            placeholder="Enter path (e.g. C:/ or //Server/Share) and press Enter...", 
            id="path_changer"
        )
        file_browser_tree = FilteredDirectoryTree(".", id="file_browser")
        file_browser_container = Vertical(path_changer_input, file_browser_tree, id="files_container")

        TS_WIDTH = 21
        PA_WIDTH = 10
        GA_WIDTH = 10
        PAYLOAD_WIDTH = 23
        COLUMN_SEPARATORS_WIDTH = 6 
        fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
        available_width = self.app.size.width
        remaining_width = available_width - fixed_width - 6 
        name_width = max(10, remaining_width // 2)
        
        self.log_widget.add_column("Timestamp", key="ts", width=TS_WIDTH)
        self.log_widget.add_column("PA", key="pa", width=PA_WIDTH)
        self.log_widget.add_column("Device (PA)", key="pa_name", width=name_width)
        self.log_widget.add_column("GA", key="ga", width=GA_WIDTH)
        self.log_widget.add_column("Group Address (GA)", key="ga_name", width=name_width)
        self.log_widget.add_column("Payload", key="payload", width=PAYLOAD_WIDTH)

        tabs.add_pane(TabPane("Building Structure", building_tree, id="building_pane"))
        tabs.add_pane(TabPane("Physical Addresses", pa_tree, id="pa_pane"))
        tabs.add_pane(TabPane("Group Addresses", ga_tree, id="ga_pane"))
        tabs.add_pane(TabPane("Selection Groups", named_filter_container, id="filter_pane"))
        tabs.add_pane(TabPane("Log View", log_view_container, id="log_pane"))
        tabs.add_pane(TabPane("Files", file_browser_container, id="files_pane"))
        
        logging.debug("build_ui_tabs: UI-Tabs erstellt.")
    
    def _reset_user_activity(self) -> None:
        logging.debug("User activity detected, resetting idle timer.")
        self.last_user_activity = time.time()
        if not self.log_reload_timer and self.log_auto_reload_enabled:
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
                    self.notify(f"Changed directory: {target_path}")
                else:
                    if os.name == 'nt' and target_path.startswith(r'\\') and len(target_path.split(os.sep)) == 3:
                         try:
                            self.query_one("#file_browser", DirectoryTree).path = target_path
                            self.notify(f"Server view opened: {target_path}")
                         except Exception as e:
                            self.notify(f"Error loading server {target_path}: {e}", severity="error")
                    else:
                        self.notify(f"Directory not found: {target_path}", severity="error")
            except Exception as e:
                self.notify(f"Path error: {e}", severity="error")
        elif event.input.id == "regex_filter_input":
            filter_text = event.value
            if not filter_text:
                self.regex_filter = None
                self.regex_filter_string = ""
                self.notify("Regex filter removed.")
            else:
                try:
                    self.regex_filter = re.compile(filter_text, re.IGNORECASE)
                    self.regex_filter_string = filter_text
                    self.notify(f"Global AND regex filter active: '{filter_text}'")
                except re.error as e:
                    self.regex_filter = None
                    self.regex_filter_string = ""
                    self.notify(f"Invalid regex: {e}", severity="error")
            self.paging_warning_shown = False
            self.log_view_is_dirty = True
            self._refilter_log_view()
    
    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._reset_user_activity() 
        event.stop()
        file_path = str(event.path)
        if file_path.lower().endswith((".log", ".zip", ".txt")):
            self.notify(f"Loading file: {os.path.basename(file_path)}")
            self.config['log_file'] = file_path
            self._reload_log_file_sync()
            
            self.query_one(TabbedContent).active = "log_pane"

    def action_toggle_selection(self) -> None:
        self._reset_user_activity() 
        try:
            # Das fokussierte Widget holen
            focused_widget = self.app.focused
            
            tree = None
            
            # Prüfen, ob das fokussierte Widget ein Baum ist (und nicht der file_browser)
            if isinstance(focused_widget, Tree) and focused_widget.id != "file_browser":
                tree = focused_widget
            else:
                # Fallback: Versuche, den Baum im aktiven Tab zu finden
                try:
                    active_pane = self.query_one(TabbedContent).active_pane
                    tree = active_pane.query_one("Tree:not(#file_browser)")
                except Exception:
                    logging.warning("Aktion 'toggle_selection' konnte keinen fokussierten oder aktiven Baum finden.")
                    return

            if not tree:
                logging.warning("Aktion 'toggle_selection' hat keinen gültigen Baum gefunden.")
                return

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
        
        # Lade Baum-Payloads bei Bedarf nach
        tree_id = f"#{pane_id.replace('_pane', '_tree')}" # z.B. "pa_pane" -> "#pa_tree"
        if tree_id in self.trees_need_payload_update:
            try:
                self.notify(f"Loading payloads for tree '{tree_id}'...")
                logging.info(f"Aktualisiere Labels (mit Payloads) für {tree_id}...")
                start_time = time.time()
                
                self._update_tree_labels_recursively(self.query_one(tree_id, Tree).root)
                
                duration = time.time() - start_time
                logging.info(f"Labels für {tree_id} in {duration:.4f}s aktualisiert.")
                self.trees_need_payload_update.remove(tree_id)
            except Exception as e:
                logging.error(f"Fehler beim Nachladen der Labels für {tree_id}: {e}")

        # 2. Fokus für Tastatureingaben setzen
        try:
            if pane_id in ("building_pane", "pa_pane", "ga_pane", "filter_pane"):
                event.pane.query_one(Tree).focus()
            elif pane_id == "log_pane":
                self.query_one("#regex_filter_input", Input).focus()
            elif pane_id == "files_pane":
                self.query_one("#file_browser", DirectoryTree).focus()
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

    def action_load_file(self) -> None:
        """
        Löst das Laden der im Datei-Browser ausgewählten Datei aus.
        Wird durch 'Enter' im 'Dateien'-Tab ausgelöst.
        """
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "files_pane":
            return
        
        try:
            tree = self.query_one("#file_browser", DirectoryTree)
            node = tree.cursor_node
            
            if node and node.data and not node.data.is_dir():
                file_path = str(node.data.path)
                logging.info(f"action_load_file: Lade Datei via 'Enter': {file_path}")
                # Logik aus on_directory_tree_file_selected duplizieren
                if file_path.lower().endswith((".log", ".zip", ".txt")):
                    self.notify(f"Loading file: {os.path.basename(file_path)}")
                    self.config['log_file'] = file_path
                    self._reload_log_file_sync()
                    self.query_one(TabbedContent).active = "log_pane"
                else:
                    self.notify("Only .log, .zip, or .txt files can be loaded.", severity="warning")
            elif node and node.data and node.data.is_dir():
                logging.debug("action_load_file: 'Enter' auf Verzeichnis, ignoriere (Standard-Toggle).")
            else:
                self.notify("No file selected.", severity="warning")
                
        except Exception as e:
            logging.error(f"Fehler in action_load_file: {e}", exc_info=True)
            self.notify(f"Error loading file: {e}", severity="error")


    def action_reload_log_file(self) -> None:
        logging.info("Log-Datei wird manuell von Festplatte neu geladen.")
        self._reload_log_file_sync()
    
    def action_save_filter(self) -> None:
        if not self.selected_gas:
            self.notify("No GAs selected, nothing to save.", severity="warning")
            return
        def save_callback(name: str):
            if not name:
                self.notify("Save canceled.", severity="warning")
                return
            new_rules = sorted(list(self.selected_gas))
            self.named_filters[name] = new_rules
            self._save_named_filters()
            self._load_named_filters()
            self._populate_named_filter_tree()
            self.notify(f"Filter '{name}' saved with {len(new_rules)} GAs.")
        self.push_screen(FilterInputScreen(prompt="Save current selection as:"), save_callback)

    def action_delete_item(self) -> None:
        try:
            active_pane = self.query_one(TabbedContent).active_pane
            if active_pane.id != "filter_pane":
                self.notify("Delete ('d') is only active in the 'Selection Groups' tab.", severity="info")
                return
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not node.data:
                self.notify("No filter selected for deletion.", severity="warning")
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
                            self.notify(f"Rule '{rule_str}' deleted from '{filter_name}'.")
                        except Exception as e:
                            self.notify(f"Error deleting rule: {e}", severity="error")
                    else:
                        self.notify("Deletion canceled.")
                self.push_screen(FilterInputScreen(prompt=f"Really delete rule '{rule_str}'? (Yes/No)"), confirm_rule_delete)
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
                            self.notify(f"Filter group '{filter_name}' deleted.")
                        except Exception as e:
                            self.notify(f"Error deleting: {e}", severity="error")
                    else:
                        self.notify("Deletion canceled.")
                self.push_screen(FilterInputScreen(prompt=f"Really delete group '{filter_name}'? (Yes/No)"), confirm_filter_delete)
        except Exception as e:
            logging.error(f"Fehler bei action_delete_item: {e}", exc_info=True)
    
    def action_toggle_log_reload(self, force_on: bool = False, force_off: bool = False) -> None:
        TIMER_INTERVAL = self.reload_interval 
        if force_off:
            if self.log_reload_timer:
                self.log_reload_timer.stop()
                self.log_reload_timer = None
                if not force_off: 
                    self.notify("Log Auto-Reload [bold red]OFF[/] (Archive/Error).", title="Log View")
                logging.info("Auto-Reload gestoppt (force_off).")
            return
        if force_on:
            self.last_user_activity = time.time() 
            if not self.log_reload_timer:
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]ON[/] ({TIMER_INTERVAL}s).", title="Log View")
                logging.info(f"Auto-Reload (effizient) für .log-Datei gestartet (Intervall: {TIMER_INTERVAL}s).")
            return
        self._reset_user_activity() 
        if self.log_reload_timer:
            self.log_reload_timer.stop()
            self.log_reload_timer = None
            self.notify("Log Auto-Reload [bold red]OFF[/].", title="Log View")
            logging.info("Auto-Reload manuell deaktiviert.")
        else:
            log_file_path = self.config.get("log_file")
            if log_file_path and log_file_path.lower().endswith((".log", ".txt")):
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]ON[/] ({TIMER_INTERVAL}s).", title="Log View")
                logging.info(f"Auto-Reload (effizient) manuell aktiviert (Intervall: {TIMER_INTERVAL}s).")
            else:
                self.notify("Auto-Reload only available for .log/.txt files.", severity="warning")
            
    def action_time_filter(self) -> None:
        self._reset_user_activity() 
        def parse_time_input(time_str: str) -> Optional[datetime_time]:
            if not time_str: return None
            try: return datetime.strptime(time_str, "%H:%M:%S").time()
            except ValueError:
                try: return datetime.strptime(time_str, "%H:%M").time()
                except ValueError:
                    self.notify(f"Invalid time format: '{time_str}'. Use HH:MM or HH:MM:SS.", severity="error", timeout=5)
                    return None
        def handle_filter_result(result: Tuple[Optional[str], Optional[str]]):
            start_str, end_str = result
            if start_str is None and end_str is None:
                self.notify("Time filter canceled.")
                return
            new_start = parse_time_input(start_str) if start_str else None
            new_end = parse_time_input(end_str) if end_str else None
            if (start_str and new_start is None) or (end_str and new_end is None): return
            self.time_filter_start = new_start
            self.time_filter_end = new_end
            if self.time_filter_start or self.time_filter_end:
                start_log = self.time_filter_start.strftime('%H:%M:%S') if self.time_filter_start else "Start"
                end_log = self.time_filter_end.strftime('%H:%M:%S') if self.time_filter_end else "End"
                logging.info(f"Zeitfilter gesetzt: {start_log} -> {end_log}")
                self.notify(f"Time filter active: {start_log} -> {end_log}")
            else:
                logging.info("Zeitfilter entfernt.")
                self.notify("Time filter removed.")
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
            self.notify("No active tree found to filter.", severity="error")
            return

        def filter_callback(filter_text: str):
            if not filter_text:
                self.action_reset_filter()
                return
            
            lower_filter_text = filter_text.lower()
            self.notify(f"Filtering tree with: '{filter_text}'...")
            logging.info(f"Baumfilterung für Tab '{tabs.active}' gestartet mit Text: '{filter_text}'")
            start_time = time.time()
            original_data = None
            
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            
            if not original_data:
                if tabs.active == "filter_pane":
                    # --- Filtern für Filter-Tab ---
                    filtered_data, has_matches = self._filter_tree_data(self.named_filters_tree_data, lower_filter_text)
                    if not has_matches:
                        self.notify(f"No matches found for '{filter_text}'.")
                    
                    self._populate_tree_from_data(tree, filtered_data or {}, expand_all=True)
                    self._update_node_and_children_prefixes(tree.root)
                    return
                
                # --- Fallback, falls kein Tab passt ---
                self.notify("No data found to filter for this tab.", severity="error")
                return
                
            # --- Filtern für Gebäude-, PA- oder GA-Tab ---
            filtered_data, has_matches = self._filter_tree_data(original_data, lower_filter_text)
            duration = time.time() - start_time
            logging.info(f"Baumfilterung abgeschlossen in {duration:.4f}s. Treffer gefunden: {has_matches}")
            
            if not has_matches:
                self.notify(f"No matches found for '{filter_text}'.")
                
            self._populate_tree_from_data(tree, filtered_data or {}, expand_all=True)
            self._update_node_and_children_prefixes(tree.root)


        self.push_screen(
            FilterInputScreen(prompt="Filter tree (Enter to confirm, ESC to cancel):"), 
            filter_callback
            )

    def action_reset_filter(self) -> None:
        """Setzt den aktuell aktiven Baum auf den ungefilterten Zustand zurück."""
        self._reset_user_activity()
        try:
            # Benutze die gleiche robuste Logik wie action_toggle_selection
            focused_widget = self.app.focused
            tabs = self.query_one(TabbedContent)
            active_tab_id = tabs.active
            tree = None

            if isinstance(focused_widget, Tree) and focused_widget.id != "file_browser":
                tree = focused_widget
            else:
                try:
                    active_pane = tabs.active_pane
                    tree = active_pane.query_one("Tree:not(#file_browser)")
                except Exception:
                    logging.debug("action_reset_filter: Kein Baum fokussiert, Aktion ignoriert.")
                    return

            if not tree:
                logging.warning("action_reset_filter konnte keinen gültigen Baum finden.")
                return
            
            original_data = None
            if active_tab_id == "building_pane": original_data = self.building_tree_data
            elif active_tab_id == "pa_pane": original_data = self.pa_tree_data
            elif active_tab_id == "ga_pane": original_data = self.ga_tree_data
            elif active_tab_id == "filter_pane":
                # Stattdessen wird der *Original-Datenstamm* für den Filterbaum geladen
                original_data = self.named_filters_tree_data
            if active_tab_id == "files_pane":
                return # Keine Aktion für den Dateibaum
                
            if original_data:
                self._populate_tree_from_data(tree, original_data, expand_all=False)
                
                # 'escape' soll nur die Präfixe basierend auf der *aktuellen*
                # Auswahl (self.selected_gas) wiederherstellen.
                self._update_node_and_children_prefixes(tree.root)

                self.notify("Tree filter reset.")
                logging.info(f"Baumfilter für {active_tab_id} zurückgesetzt.")
            
        except Exception as e:
            logging.error(f"Fehler bei action_reset_filter: {e}", exc_info=True)
            self.notify("Error resetting filter.", severity="error")

    def action_reload_filter_tree(self) -> None:
        """Lädt die named_filters.yaml neu ein (Taste 'l')."""
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane":
            return
        try:
            self._load_named_filters()
            self._populate_named_filter_tree()
            self.notify(f"Filter file '{self.named_filter_path.name}' reloaded.")
            logging.info("Named-Filter-Datei neu geladen.")
        except Exception as e:
            self.notify(f"Error reloading filters: {e}", severity="error")
            logging.error(f"Fehler bei action_reload_filter_tree: {e}", exc_info=True)

    def action_clear_selection(self) -> None:
        """Leert die gesamte GA-Auswahl (Taste 'c')."""
        self._reset_user_activity()
        if not self.selected_gas and not self.active_named_filters:
            self.notify("Selection is already empty.", severity="information")
            return
        
        logging.info("Lösche gesamte GA-Auswahl ('c').")
        self.selected_gas.clear()
        self.active_named_filters.clear()
        self._rebuild_active_regexes()
        
        # Das ist der entscheidende Teil:
        self._update_all_tree_prefixes()
        
        self.log_view_is_dirty = True
        if self.query_one(TabbedContent).active == "log_pane":
            self._refilter_log_view()
        
        self.notify("Entire selection cleared.")

    def action_new_rule(self) -> None:
        """Fügt eine neue Regel zu einem bestehenden Filter hinzu (Taste 'n')."""
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane":
            return
        
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not node.data:
                self.notify("Please select a filter group first.", severity="warning")
                return

            filter_name = ""
            if isinstance(node.data, tuple): # Kind-Knoten (Regel)
                filter_name = node.data[0]
            elif isinstance(node.data, str): # Eltern-Knoten (Gruppe)
                filter_name = node.data
            
            if not filter_name:
                self.notify("Node data invalid, cannot find filter name.", severity="error")
                return
            
            def add_rule_callback(rule_str: str):
                if not rule_str:
                    self.notify("Add operation canceled.", severity="warning")
                    return
                
                try:
                    if filter_name not in self.named_filters:
                        self.named_filters[filter_name] = []
                    
                    self.named_filters[filter_name].append(rule_str)
                    self._save_named_filters()
                    self._load_named_filters()
                    self._populate_named_filter_tree()
                    self.notify(f"Rule '{rule_str}' added to '{filter_name}'.")
                    logging.info(f"Regel '{rule_str}' zu Filter '{filter_name}' hinzugefügt.")
                except Exception as e:
                    self.notify(f"Error saving rule: {e}", severity="error")
                    logging.error(f"Fehler in add_rule_callback: {e}", exc_info=True)

            self.push_screen(FilterInputScreen(prompt=f"New rule for '{filter_name}' (GA or Regex):"), add_rule_callback)

        except Exception as e:
            logging.error(f"Fehler bei action_new_rule: {e}", exc_info=True)
            self.notify(f"Error starting 'New Rule': {e}", severity="error")

    def action_new_filter_group(self) -> None:
        """Erstellt eine neue, leere Filter-Gruppe (Taste 'ctrl+n')."""
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane":
            return
        
        try:
            def add_group_callback(group_name: str):
                if not group_name:
                    self.notify("Creation canceled.", severity="warning")
                    return
                
                if group_name in self.named_filters:
                    self.notify(f"Error: Group '{group_name}' already exists.", severity="error")
                    return
                
                try:
                    self.named_filters[group_name] = [] # Leere Liste als Platzhalter
                    self._save_named_filters()
                    self._load_named_filters() # Neu laden, um Regeln zu kompilieren (obwohl hier leer)
                    self._populate_named_filter_tree()
                    self.notify(f"New filter group '{group_name}' created.")
                    logging.info(f"Neue Filter-Gruppe '{group_name}' erstellt.")
                except Exception as e:
                    self.notify(f"Error saving group: {e}", severity="error")
                    logging.error(f"Fehler in add_group_callback: {e}", exc_info=True)

            self.push_screen(FilterInputScreen(prompt="Name for new selection group:"), add_group_callback)

        except Exception as e:
            logging.error(f"Fehler bei action_new_filter_group: {e}", exc_info=True)
            self.notify(f"Error starting 'New Group': {e}", severity="error")

    def action_edit_rule(self) -> None:
        """Bearbeitet eine bestehende Regel (Taste 'e')."""
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane":
            return
        
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            
            # Muss ein Kind-Knoten (eine Regel) sein
            if not node or not isinstance(node.data, tuple):
                self.notify("Please select a rule (child node) to edit.", severity="warning")
                return

            filter_name, old_rule_str = node.data
            
            def edit_rule_callback(new_rule_str: str):
                if not new_rule_str or new_rule_str == old_rule_str:
                    self.notify("Edit canceled or no change.", severity="warning")
                    return
                
                try:
                    # Finde und ersetze die alte Regel
                    if filter_name in self.named_filters and old_rule_str in self.named_filters[filter_name]:
                        index = self.named_filters[filter_name].index(old_rule_str)
                        self.named_filters[filter_name][index] = new_rule_str
                        
                        self._save_named_filters()
                        self._load_named_filters()
                        self._populate_named_filter_tree()
                        self.notify(f"Rule edited: '{old_rule_str}' -> '{new_rule_str}'")
                        logging.info(f"Regel in '{filter_name}' bearbeitet: '{old_rule_str}' -> '{new_rule_str}'")
                    else:
                        self.notify("Error: Old rule could not be found.", severity="error")
                        logging.warning(f"Konnte alte Regel '{old_rule_str}' in '{filter_name}' nicht finden.")

                except Exception as e:
                    self.notify(f"Error saving rule: {e}", severity="error")
                    logging.error(f"Fehler in edit_rule_callback: {e}", exc_info=True)

            # Übergibt den alten Wert direkt an den Screen
            self.push_screen(
                FilterInputScreen(prompt=f"Edit rule (for '{filter_name}'):", initial_value=old_rule_str), 
                edit_rule_callback
            )

        except Exception as e:
            logging.error(f"Fehler bei action_edit_rule: {e}", exc_info=True)
            self.notify(f"Error starting 'Edit Rule': {e}", severity="error")


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
            print("WARNING: 'knx-lens.css' not found in the same directory.", file=sys.stderr)
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
            print("ERROR: Project path not found. Please run 'setup.py' or specify with --path.", file=sys.stderr)
            sys.exit(1)
        app = KNXLens(config=config)
        app.run()
    except Exception:
        logging.critical("Unbehandelter Fehler in der main() Funktion", exc_info=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()  