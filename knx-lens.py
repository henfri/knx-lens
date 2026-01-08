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
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    sys.exit(1)

from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Header, Tree, Static, TabbedContent, TabPane, DataTable, DirectoryTree, Input
from textual.widgets.tree import TreeNode
from textual import events
from textual.timer import Timer

from knx_project_utils import (
    load_or_parse_project, 
    build_ga_tree_data, 
    build_pa_tree_data, 
    build_building_tree_data
)
from knx_log_utils import parse_and_cache_log_data
from knx_tui_screens import FilterInputScreen, TimeFilterScreen, FilteredDirectoryTree

from knx_tui_logic import KNXTuiLogic

LOG_LEVEL = logging.DEBUG
TreeData = Dict[str, Any]

# Binding-Definitionen
binding_i_time_filter = Binding("i", "time_filter", "Time Filter", show=False)
binding_enter_load_file = Binding("enter", "load_file", "Load File", show=False)
binding_l_reload_filters = Binding("l", "reload_filter_tree", "Reload Groups", show=False)
binding_c_clear_selection = Binding("c", "clear_selection", "Clear Selection", show=False)
binding_n_new_rule = Binding("n", "new_rule", "New Rule", show=False)
binding_e_edit_rule = Binding("e", "edit_rule", "Edit Rule", show=False)
binding_m_new_group = Binding("m", "new_filter_group", "New Group", show=False)
binding_g_refresh_files = Binding("g", "refresh_files", "Refresh Files", show=False)

### --- TUI: HAUPTANWENDUNG ---
class KNXLens(App, KNXTuiLogic):
    CSS_PATH = "knx-lens.css"
    
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("a", "toggle_selection", "Select", show=False),
        Binding("s", "save_filter", "Save Selection", show=False),
        Binding("f", "filter_tree", "Filter Tree", show=False),
        Binding("escape", "reset_filter", "Reset Filter", show=False),
        Binding("d", "delete_item", "Delete", show=False),
        Binding("r", "reload_log_file", "Reload", show=False),
        Binding("t", "toggle_log_reload", "Auto-Reload", show=False),
        binding_i_time_filter,
        binding_enter_load_file,
        binding_l_reload_filters,
        binding_c_clear_selection,
        binding_n_new_rule,
        binding_e_edit_rule,
        binding_m_new_group,
        binding_g_refresh_files
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
        self.stats_pa_ga_data: TreeData = {}
        self.stats_ga_pa_data: TreeData = {}
        self.stats_needs_update: bool = True
        self.cached_log_data: List[Dict[str, str]] = []
        self.time_filter_start: Optional[datetime_time] = None
        self.time_filter_end: Optional[datetime_time] = None
        self.last_user_activity: float = time.time()
        self.log_view_is_dirty: bool = True 
        self.last_log_mtime: Optional[float] = None
        self.last_log_position: int = 0
        self.last_log_size: int = 0
        self.paging_warning_shown: bool = False
        self.max_log_lines = int(self.config.get('max_log_lines', 10000))
        self.reload_interval = float(self.config.get('reload_interval', 1.0))

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
            "stats_pane": [
                Binding("r", "reload_log_file", "Reload"),
                binding_i_time_filter,
            ],
            "log_pane": [
                Binding("r", "reload_log_file", "Reload"),
                Binding("t", "toggle_log_reload", "Auto-Reload"),
                binding_i_time_filter,
            ],
            "files_pane": [
                binding_enter_load_file,
                binding_g_refresh_files,
                binding_i_time_filter,
            ] 
        }
        
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
        
        try:
            self.project_data = load_or_parse_project(self.config['knxproj_path'], self.config['password'])
            knxproj_dir = Path(self.config['knxproj_path']).parent
            self.named_filter_path = knxproj_dir / "named_filters.yaml"
            
            self.ga_tree_data = build_ga_tree_data(self.project_data)
            self.pa_tree_data = build_pa_tree_data(self.project_data)
            self.building_tree_data = build_building_tree_data(self.project_data)

            self.build_ui_tabs()
            
            self.query_one("#loading_container").remove()
            tabs = self.query_one(TabbedContent)
            tabs.disabled = False
            tabs.focus()

            self.notify("Project loaded. Loading logs in the background...")
            self.call_later(self.load_data_phase_2)
            
            self.update_footer("building_pane")
            self.query_one("#manual_footer", Static).styles.dock = "bottom"
            
        except Exception as e:
            self.show_startup_error(e, traceback.format_exc())
    
    def load_data_phase_2(self) -> None:
        logging.debug("load_data_phase_2: Starte Phase 2 (Log-Laden)...")
        try:
            self._load_named_filters()
            self._load_log_file_data_only()

            self._populate_tree_from_data(self.query_one("#building_tree", Tree), self.building_tree_data)
            self._populate_tree_from_data(self.query_one("#pa_tree", Tree), self.pa_tree_data)
            self._populate_tree_from_data(self.query_one("#ga_tree", Tree), self.ga_tree_data)
            self._populate_named_filter_tree()

            self._update_tree_labels_recursively(self.query_one("#building_tree", Tree).root)

            self.log_view_is_dirty = True
            self._process_log_lines()
            self.log_view_is_dirty = False 

            if not (self.config.get("log_file") or "").lower().endswith(".zip"):
                 self.action_toggle_log_reload(force_on=True)
            
            self.query_one(TabbedContent).active = "building_pane"
            
        except Exception as e:
            logging.error(f"Fehler in Phase 2: {e}", exc_info=True)
            self.notify(f"Error loading log file: {e}", severity="error")

    def build_ui_tabs(self) -> None:
        tabs = self.query_one(TabbedContent)
        
        building_tree = Tree("Building", id="building_tree")
        pa_tree = Tree("Topology", id="pa_tree")
        ga_tree = Tree("Functions", id="ga_tree")
        filter_tree = Tree("Selection Groups", id="named_filter_tree")
        named_filter_container = Vertical(filter_tree, id="named_filter_container")
        
        # Statistics Tab: ein Tree mit zwei Hauptknoten (GA → PA und PA → GA)
        stats_tree = Tree("Statistics", id="stats_tree")
        
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
        
        # --- HIER DIE ANPASSUNG DER ÜBERSCHRIFTEN ---
        self.log_widget.add_column("Timestamp", key="ts", width=TS_WIDTH)
        self.log_widget.add_column("PA", key="pa", width=PA_WIDTH)
        self.log_widget.add_column("Device-Name", key="pa_name", width=name_width) # <-- Name statt "Device (PA)"
        self.log_widget.add_column("GA", key="ga", width=GA_WIDTH)
        self.log_widget.add_column("GA-Name", key="ga_name", width=name_width)     # <-- Name statt "Group Address (GA)"
        self.log_widget.add_column("Payload", key="payload", width=PAYLOAD_WIDTH)

        tabs.add_pane(TabPane("Building Structure", building_tree, id="building_pane"))
        tabs.add_pane(TabPane("Physical Addresses", pa_tree, id="pa_pane"))
        tabs.add_pane(TabPane("Group Addresses", ga_tree, id="ga_pane"))
        tabs.add_pane(TabPane("Selection Groups", named_filter_container, id="filter_pane"))
        tabs.add_pane(TabPane("Statistics", stats_tree, id="stats_pane"))
        tabs.add_pane(TabPane("Log View", log_view_container, id="log_pane"))
        tabs.add_pane(TabPane("Files", file_browser_container, id="files_pane"))
    
    def _reset_user_activity(self) -> None:
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
                    target_path = str(Path(raw_input).resolve())
                if os.path.isdir(target_path):
                    self.query_one("#file_browser", DirectoryTree).path = target_path
                    self.notify(f"Changed directory: {target_path}")
                else:
                    if os.name == 'nt' and target_path.startswith(r'\\'):
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

    def action_refresh_files(self) -> None:
        if self.query_one(TabbedContent).active != "files_pane": return
        self._reset_user_activity()
        try:
            tree = self.query_one("#file_browser", DirectoryTree)
            current_path = tree.path
            tree.path = str(Path(current_path).absolute())
            tree.reload()
            self.notify("File list refreshed.")
            logging.info("File Browser refreshed.")
        except Exception as e:
            self.notify(f"Error refreshing files: {e}", severity="error")

    def action_toggle_selection(self) -> None:
        self._reset_user_activity() 
        try:
            focused_widget = self.app.focused
            tree = None
            if isinstance(focused_widget, Tree) and focused_widget.id != "file_browser":
                tree = focused_widget
            else:
                try:
                    active_pane = self.query_one(TabbedContent).active_pane
                    tree = active_pane.query_one("Tree:not(#file_browser)")
                except Exception:
                    return

            if not tree: return

            node = tree.cursor_node
            if not node: return
            if tree.id == "named_filter_tree":
                if not node.data: return
                filter_name = node.data[0] if isinstance(node.data, tuple) else str(node.data)
                rules = self.named_filters_rules.get(filter_name)
                if not rules: return
                if filter_name in self.active_named_filters:
                    self.active_named_filters.remove(filter_name)
                    self.selected_gas.difference_update(rules["gas"])
                else:
                    self.active_named_filters.add(filter_name)
                    self.selected_gas.update(rules["gas"])
                self._rebuild_active_regexes()
                self._update_all_tree_prefixes()
            elif tree.id != "file_browser":
                descendant_gas = self._get_descendant_gas(node)
                if not descendant_gas and (not node.parent or node.parent.id == "#tree-root"):
                     for child in node.children:
                        descendant_gas.update(self._get_descendant_gas(child))
                elif not descendant_gas: return
                
                if descendant_gas.issubset(self.selected_gas):
                    self.selected_gas.difference_update(descendant_gas)
                else:
                    self.selected_gas.update(descendant_gas)
                
                self._update_node_and_children_prefixes(node)
                if node.parent: self._update_parent_prefixes_recursive(node.parent)
                self._update_named_filter_prefixes()
                
                # Update labels to preserve payload history
                self._update_tree_labels_recursively(node)
            
            self.log_view_is_dirty = True
            if self.query_one(TabbedContent).active == "log_pane":
                self._refilter_log_view()
        except Exception as e:
            logging.error(f"Fehler bei action_toggle_selection: {e}", exc_info=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._reset_user_activity() 
        pane_id = event.pane.id
        self.update_footer(pane_id)
        
        # Statistics tab: berechne Stats on demand
        if pane_id == "stats_pane":
            if self.stats_needs_update:
                self.notify("Computing statistics...")
                self.stats_pa_ga_data = self._build_statistics_tree_data_pa_ga()
                self.stats_ga_pa_data = self._build_statistics_tree_data_ga_pa()
                self._populate_statistics_combined(self.query_one("#stats_tree", Tree), self.stats_pa_ga_data, self.stats_ga_pa_data)
                self.stats_needs_update = False
                self.notify("Statistics computed.")
            try:
                self.query_one("#stats_tree", Tree).focus()
            except Exception: pass
            return
        
        tree_id = f"#{pane_id.replace('_pane', '_tree')}"
        if tree_id in self.trees_need_payload_update:
            try:
                self.notify(f"Loading payloads for tree '{tree_id}'...")
                self._update_tree_labels_recursively(self.query_one(tree_id, Tree).root)
                self.trees_need_payload_update.remove(tree_id)
            except Exception: pass

        try:
            if pane_id in ("building_pane", "pa_pane", "ga_pane", "filter_pane"):
                event.pane.query_one(Tree).focus()
            elif pane_id == "log_pane":
                self.log_widget.focus()
            elif pane_id == "files_pane":
                self.query_one("#file_browser", DirectoryTree).focus()
        except Exception: pass

        if event.pane.id == "log_pane" and self.log_view_is_dirty:
            self._refilter_log_view()

    def update_footer(self, pane_id: str) -> None:
        try:
            footer_static = self.query_one("#manual_footer", Static)
            global_bindings = self.global_bindings_display
            context_bindings = self.tab_bindings_display.get(pane_id, [])
            all_bindings = global_bindings + context_bindings
            footer_text = "  ".join(f"[bold]{b.key.upper()}[/]:{b.description}" for b in all_bindings)
            footer_static.update(footer_text)
        except Exception: pass 

    def action_load_file(self) -> None:
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "files_pane": return
        try:
            tree = self.query_one("#file_browser", DirectoryTree)
            node = tree.cursor_node
            if node and node.data and not node.data.is_dir():
                file_path = str(node.data.path)
                if file_path.lower().endswith((".log", ".zip", ".txt")):
                    self.notify(f"Loading file: {os.path.basename(file_path)}")
                    self.config['log_file'] = file_path
                    self._reload_log_file_sync()
                    self.query_one(TabbedContent).active = "log_pane"
                else:
                    self.notify("Only .log, .zip, or .txt files can be loaded.", severity="warning")
            elif node and node.data and node.data.is_dir(): pass
            else: self.notify("No file selected.", severity="warning")
        except Exception as e:
            self.notify(f"Error loading file: {e}", severity="error")

    def action_reload_log_file(self) -> None:
        self._reload_log_file_sync()
    
    def action_save_filter(self) -> None:
        if not self.selected_gas:
            self.notify("No GAs selected, nothing to save.", severity="warning")
            return
        def save_callback(name: str):
            if not name: return
            new_rules = sorted(list(self.selected_gas))
            self.named_filters[name] = new_rules
            self._save_named_filters()
            self._load_named_filters()
            self._populate_named_filter_tree()
            self.notify(f"Filter '{name}' saved.")
        self.push_screen(FilterInputScreen(prompt="Save current selection as:"), save_callback)

    def action_delete_item(self) -> None:
        try:
            if self.query_one(TabbedContent).active != "filter_pane": return
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not node.data: return
            
            if isinstance(node.data, tuple):
                filter_name, rule_str = node.data
                def confirm_rule_delete(confirm: str):
                    if confirm.lower() in ["ja", "j", "yes", "y"]:
                        self.named_filters[filter_name].remove(rule_str)
                        self._save_named_filters()
                        self._load_named_filters()
                        self._populate_named_filter_tree()
                        self.notify(f"Rule '{rule_str}' deleted.")
                self.push_screen(FilterInputScreen(prompt=f"Delete rule '{rule_str}'? (y/n)"), confirm_rule_delete)
            elif isinstance(node.data, str):
                filter_name = str(node.data)
                def confirm_filter_delete(confirm: str):
                    if confirm.lower() in ["ja", "j", "yes", "y"]:
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
                        self.notify(f"Group '{filter_name}' deleted.")
                self.push_screen(FilterInputScreen(prompt=f"Delete group '{filter_name}'? (y/n)"), confirm_filter_delete)
        except Exception: pass
    
    def action_toggle_log_reload(self, force_on: bool = False, force_off: bool = False) -> None:
        TIMER_INTERVAL = self.reload_interval 
        if force_off:
            if self.log_reload_timer:
                self.log_reload_timer.stop()
                self.log_reload_timer = None
                self.notify("Log Auto-Reload [bold red]OFF[/].", title="Log View")
            return
        if force_on:
            self.last_user_activity = time.time() 
            if not self.log_reload_timer:
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]ON[/] ({TIMER_INTERVAL}s).", title="Log View")
            return
        self._reset_user_activity() 
        if self.log_reload_timer:
            self.log_reload_timer.stop()
            self.log_reload_timer = None
            self.notify("Log Auto-Reload [bold red]OFF[/].", title="Log View")
        else:
            if (self.config.get("log_file") or "").lower().endswith((".log", ".txt")):
                self.log_reload_timer = self.set_interval(TIMER_INTERVAL, self._efficient_log_tail)
                self.notify(f"Log Auto-Reload [bold green]ON[/] ({TIMER_INTERVAL}s).", title="Log View")
            else:
                self.notify("Auto-Reload only available for .log/.txt files.", severity="warning")
            
    def action_time_filter(self) -> None:
        self._reset_user_activity() 
        def parse_time_input(time_str: str) -> Optional[datetime_time]:
            if not time_str: return None
            try: return datetime.strptime(time_str, "%H:%M:%S").time()
            except ValueError:
                try: return datetime.strptime(time_str, "%H:%M").time()
                except ValueError: return None
        def handle_filter_result(result: Tuple[Optional[str], Optional[str]]):
            start_str, end_str = result
            if start_str is None and end_str is None: return
            new_start = parse_time_input(start_str) if start_str else None
            new_end = parse_time_input(end_str) if end_str else None
            self.time_filter_start = new_start
            self.time_filter_end = new_end
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
        except Exception: return

        def filter_callback(filter_text: str):
            if not filter_text:
                self.action_reset_filter()
                return
            lower_filter_text = filter_text.lower()
            original_data = None
            if tabs.active == "building_pane": original_data = self.building_tree_data
            elif tabs.active == "pa_pane": original_data = self.pa_tree_data
            elif tabs.active == "ga_pane": original_data = self.ga_tree_data
            elif tabs.active == "filter_pane": original_data = self.named_filters_tree_data
            
            if not original_data: return
                
            filtered_data, _ = self._filter_tree_data(original_data, lower_filter_text)
            self._populate_tree_from_data(tree, filtered_data or {}, expand_all=True)
            self._update_node_and_children_prefixes(tree.root)

        self.push_screen(FilterInputScreen(prompt="Filter tree:"), filter_callback)

    def action_reset_filter(self) -> None:
        self._reset_user_activity()
        try:
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
                except Exception: return

            if not tree: return
            
            original_data = None
            if active_tab_id == "building_pane": original_data = self.building_tree_data
            elif active_tab_id == "pa_pane": original_data = self.pa_tree_data
            elif active_tab_id == "ga_pane": original_data = self.ga_tree_data
            elif active_tab_id == "filter_pane": original_data = self.named_filters_tree_data
                
            if original_data:
                self._populate_tree_from_data(tree, original_data, expand_all=False)
                self._update_node_and_children_prefixes(tree.root)
                self.notify("Tree filter reset.")
        except Exception: pass

    def action_reload_filter_tree(self) -> None:
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane": return
        try:
            self._load_named_filters()
            self._populate_named_filter_tree()
            self.notify(f"Filters reloaded.")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_clear_selection(self) -> None:
        self._reset_user_activity()
        if not self.selected_gas and not self.active_named_filters: return
        self.selected_gas.clear()
        self.active_named_filters.clear()
        self._rebuild_active_regexes()
        self._update_all_tree_prefixes()
        self.log_view_is_dirty = True
        if self.query_one(TabbedContent).active == "log_pane":
            self._refilter_log_view()
        self.notify("Selection cleared.")

    def action_new_rule(self) -> None:
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane": return
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not node.data: return
            filter_name = node.data[0] if isinstance(node.data, tuple) else node.data
            
            def add_rule_callback(rule_str: str):
                if not rule_str: return
                if filter_name not in self.named_filters: self.named_filters[filter_name] = []
                self.named_filters[filter_name].append(rule_str)
                self._save_named_filters()
                self._load_named_filters()
                self._populate_named_filter_tree()
                self.notify(f"Rule added.")
            self.push_screen(FilterInputScreen(prompt=f"New rule for '{filter_name}':"), add_rule_callback)
        except Exception: pass

    def action_new_filter_group(self) -> None:
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane": return
        def add_group_callback(group_name: str):
            if not group_name or group_name in self.named_filters: return
            self.named_filters[group_name] = [] 
            self._save_named_filters()
            self._load_named_filters() 
            self._populate_named_filter_tree()
            self.notify(f"Group '{group_name}' created.")
        self.push_screen(FilterInputScreen(prompt="New group name:"), add_group_callback)

    def action_edit_rule(self) -> None:
        self._reset_user_activity()
        if self.query_one(TabbedContent).active != "filter_pane": return
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            node = tree.cursor_node
            if not node or not isinstance(node.data, tuple): return
            filter_name, old_rule_str = node.data
            
            def edit_rule_callback(new_rule_str: str):
                if not new_rule_str or new_rule_str == old_rule_str: return
                if filter_name in self.named_filters and old_rule_str in self.named_filters[filter_name]:
                    index = self.named_filters[filter_name].index(old_rule_str)
                    self.named_filters[filter_name][index] = new_rule_str
                    self._save_named_filters()
                    self._load_named_filters()
                    self._populate_named_filter_tree()
                    self.notify("Rule edited.")
            self.push_screen(FilterInputScreen(prompt="Edit rule:", initial_value=old_rule_str), edit_rule_callback)
        except Exception: pass

    def on_resize(self, event: events.Resize) -> None:
        if not self.log_widget: return
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
        except KeyError: pass

def main():
    try:
        logging.basicConfig(
            level=LOG_LEVEL, 
            filename='knx_lens.log', 
            filemode='w',
            format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
            encoding='utf-8'
        )
        load_dotenv()
        parser = argparse.ArgumentParser(description="KNX-Lens")
        parser.add_argument("--path", help="Path to .knxproj")
        parser.add_argument("--log-file", help="Path to log file")
        parser.add_argument("--password", help="Password")
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
            print("ERROR: Project path not found.", file=sys.stderr)
            sys.exit(1)
        app = KNXLens(config=config)
        app.run()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()