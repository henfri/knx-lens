#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Logik-Mixin für KNX-Lens.
Enthält alle Helferfunktionen für Datenverarbeitung, Filtern und UI-Updates.
"""

import logging
import os
import re
import time
import zipfile
import io
import yaml
import traceback
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime, time as datetime_time

from textual.widgets import Tree, DataTable, TabbedContent
from textual.widgets.tree import TreeNode

from knx_log_utils import parse_and_cache_log_data, append_new_log_lines

# ============================================================================
# CONSTANTS & TYPE DEFINITIONS
# ============================================================================

TreeData = Dict[str, Any]

# Cache & Performance Settings
MAX_CACHE_SIZE = 50000  # Maximum log lines to keep in memory
PAYLOAD_HISTORY_LIMIT = 3  # Number of previous payloads to show
TREE_UPDATE_BATCH_SIZE = 100  # Nodes to update before yielding control

# Statistics sizing (Telegram length estimation)
FRAME_OVERHEAD = 10
DEFAULT_PAYLOAD_SIZE = 1
DEFAULT_FRAME_SIZE = FRAME_OVERHEAD + DEFAULT_PAYLOAD_SIZE
DPT_SIZE_MAP = {
    1: 0,
    2: 0,
    3: 0,
    4: 1,
    5: 1,
    6: 1,
    7: 2,
    8: 2,
    9: 2,
    10: 3,
    11: 3,
    12: 4,
    13: 4,
    14: 4,
    15: 4,
    16: 14,
    17: 1,
    18: 1,
    19: 8,
    20: 1,
    232: 3,
}

# File & Configuration
NAMED_FILTER_FILENAME = "named_filters.yaml"
NAMED_FILTER_DEFAULT_PATH = "named_filters.yaml"
TREE_CACHE_SUFFIX = ".tree_cache.json"

# Tree Node Keys
TREE_KEY_CHILDREN = "children"
TREE_KEY_NAME = "name"
TREE_KEY_ORIGINAL_NAME = "original_name"
TREE_KEY_GAS = "gas"
TREE_KEY_NODE_ID = "node_id"
TREE_KEY_DATA = "data" 

class KNXTuiLogic:
    """
    Diese Klasse enthält die gesamte "Business-Logik" der App.
    """

    @staticmethod
    def _truncate_payload(payload: str, max_len: int = 23) -> str:
        """Shorten payload string for display in log table."""
        if payload.startswith("ControlDimming(") and payload.endswith(")"):
            payload = payload[15:-1]
            # Enum-Darstellung auflösen: <Step.INCREASE: True> → INCREASE
            payload = re.sub(r'<\w+\.(\w+):\s*[^>]+>', r'\1', payload)
            payload = payload.replace("control=", "").replace("step_code=", "step=")
            payload = payload.replace("STEPCODE_", "")
        if len(payload) > max_len:
            payload = payload[:max_len - 3] + "..."
        return payload

    # --- DATEN-LADE-LOGIK ---

    def _load_log_file_data_only(self) -> Tuple[bool, Optional[Exception]]:
        """
        [SYNCHRON]
        Liest die Log-Datei von der Festplatte.
        """
        log_file_path = self.config.get("log_file") or os.path.join(self.config.get("log_path", "."), "knx_bus.log")
        # Persist resolved path so action_toggle_log_reload can check the extension
        self.config["log_file"] = log_file_path

        self.last_log_mtime = None
        self.last_log_position = 0
        self.last_log_size = 0 

        if not os.path.exists(log_file_path):
            logging.warning(f"Log-Datei nicht gefunden unter '{log_file_path}'")
            self.cached_log_data = []
            self.payload_history.clear()
            return False, FileNotFoundError(f"Log-Datei nicht gefunden: {log_file_path}")
        
        start_time = time.time()
        logging.info(f"Lese Log-Datei von Festplatte: '{log_file_path}'")
        
        is_zip = False
        try:
            is_zip = log_file_path.lower().endswith(".zip")
            
            lines = []
            if is_zip:
                with zipfile.ZipFile(log_file_path, 'r') as zf:
                    log_files_in_zip = [name for name in zf.namelist() if name.lower().endswith('.log')]
                    if not log_files_in_zip:
                        raise FileNotFoundError("Keine .log-Datei im ZIP-Archiv gefunden.")
                    
                    # --- FIX 3: Sicheres Lesen aus ZIP ---
                    with zf.open(log_files_in_zip[0]) as log_file:
                        # Wir lesen bytes, da TextIWrapper im zip context zickig sein kann
                        content = log_file.read()
                        # Versuch UTF-8, Fallback auf Latin-1 (Windows CP1252)
                        try:
                            decoded_text = content.decode('utf-8')
                        except UnicodeDecodeError:
                            decoded_text = content.decode('latin-1', errors='replace')
                        
                        lines = decoded_text.splitlines(keepends=True)

            else:
                self.last_log_size = os.path.getsize(log_file_path)
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    self.last_log_position = f.tell()
                self.last_log_mtime = os.path.getmtime(log_file_path)
            
            self.payload_history, self.cached_log_data = parse_and_cache_log_data(
                lines, 
                self.project_data,
                self.time_filter_start,
                self.time_filter_end
            )
            
            if len(self.cached_log_data) > MAX_CACHE_SIZE:
                self.cached_log_data = self.cached_log_data[-MAX_CACHE_SIZE:]
            
            duration = time.time() - start_time
            logging.info(f"Log-Datei '{os.path.basename(log_file_path)}' in {duration:.2f}s gelesen.")
            return is_zip, None

        except Exception as e:
            logging.error(f"Fehler beim Verarbeiten von '{log_file_path}': {e}", exc_info=True)
            self.cached_log_data = []
            self.payload_history.clear()
            return is_zip, e

    def _reload_log_file_sync(self):
        """
        [SYNCHRON]
        Wird bei neuer Datei oder 'r' aufgerufen.
        """
        self._reset_user_activity() 
        logging.debug("_reload_log_file_sync: Starte synchrones Neuladen...")
        
        reload_start_time = time.time()
        is_zip, error = self._load_log_file_data_only()
        
        if error:
            if isinstance(error, FileNotFoundError):
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]FEHLER: {error}[/red]")
            else:
                self.log_widget.clear()
                self.log_widget.add_row(f"\n[red]Fehler beim Verarbeiten der Log-Datei: {error}[/red]")
                self.log_widget.add_row(f"[dim]{traceback.format_exc()}[/dim]")
            return

        logging.info("Log-Daten neu geladen. Aktualisiere UI...")
        
        self.trees_need_payload_update = {"#pa_tree", "#ga_tree"}
        self.stats_needs_update = True
        try:
            self._update_tree_labels_recursively(self.query_one("#building_tree", Tree).root)
        except Exception: pass

        self.log_view_is_dirty = True
        self._process_log_lines()
        self.log_view_is_dirty = False 

        if is_zip:
            self.action_toggle_log_reload(force_off=True)
        else:
            self.action_toggle_log_reload(force_on=True)
        
        logging.info(f"Gesamtes Neuladen (sync) dauerte {time.time() - reload_start_time:.4f}s")

    # --- LOG-TABELLEN-LOGIK ---

    def _process_log_lines(self):
        if not self.log_widget: return
        if not self.log_caption_label: return
        
        try:
            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            
            self.log_widget.clear()
            has_ga_filter = bool(self.selected_gas)
            has_named_regex_filter = bool(self.active_named_regex_rules)
            has_global_regex_filter = bool(self.regex_filter)
            has_any_or_filter = has_ga_filter or has_named_regex_filter
    
            if not self.cached_log_data:
                 self.log_widget.add_row("[yellow]No log data loaded or log file is empty.[/yellow]")
                 self.log_caption_label.update("No log data")
                 return
            
            start_time = time.time()
            log_entries_to_process = self.cached_log_data 
            
            rows_to_add = []
            
            for i, log_entry in enumerate(log_entries_to_process):
                show_line = not has_any_or_filter
                if has_any_or_filter:
                    if has_ga_filter and log_entry["ga"] in self.selected_gas:
                        show_line = True
                    elif has_named_regex_filter and not show_line:
                        for rule in self.active_named_regex_rules:
                            if rule.search(log_entry["search_string"]):
                                show_line = True
                                break
                if not show_line: continue
                if has_global_regex_filter:
                    if not self.regex_filter.search(log_entry["search_string"]):
                        continue
                
                payload = self._truncate_payload(log_entry["payload"])
                
                rows_to_add.append((
                    log_entry["timestamp"], log_entry["pa"], log_entry["pa_name"],
                    log_entry["ga"], log_entry["ga_name"], payload
                ))
            
            found_count = len(rows_to_add)
            if found_count > self.max_log_lines:
                rows_to_add = rows_to_add[-self.max_log_lines:]
                if not self.paging_warning_shown:
                    self.paging_warning_shown = True 

            self.log_widget.add_rows(rows_to_add)
            
            duration = time.time() - start_time
            caption_str = f"{len(rows_to_add)} entries shown. ({duration:.2f}s)"
            self.log_caption_label.update(caption_str)

            if is_at_bottom:
                self.log_widget.scroll_end(animate=False, duration=0.0)

        except Exception as e:
            logging.error(f"Schwerer Fehler in _process_log_lines: {e}", exc_info=True)
            if self.log_widget:
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]Error processing log lines: {e}[/red]")

    def _efficient_log_tail(self) -> None:
        idle_duration = time.time() - self.last_user_activity
        if idle_duration > 3600:
            self.action_toggle_log_reload(force_off=True) 
            return 

        log_file_path = self.config.get("log_file")
        if not log_file_path or not log_file_path.lower().endswith((".log", ".txt")):
            self.action_toggle_log_reload(force_off=True)
            return

        try:
            try:
                current_size = os.path.getsize(log_file_path)
            except FileNotFoundError:
                return

            if current_size < self.last_log_size:
                self._reload_log_file_sync()
                return

            current_mtime = os.path.getmtime(log_file_path)
            if current_mtime == self.last_log_mtime and current_size == self.last_log_size:
                return 
            
            self.last_log_mtime = current_mtime
            self.last_log_size = current_size

            with open(log_file_path, 'r', encoding='utf-8') as f:
                f.seek(self.last_log_position)
                new_lines = f.readlines()
                self.last_log_position = f.tell()
            
            if not new_lines:
                return
            
            new_cached_items = append_new_log_lines(
                new_lines, 
                self.project_data,
                self.payload_history,
                self.cached_log_data,
                self.time_filter_start,
                self.time_filter_end
            )
            
            if len(self.cached_log_data) > MAX_CACHE_SIZE:
                 trim_amount = len(self.cached_log_data) - MAX_CACHE_SIZE
                 self.cached_log_data = self.cached_log_data[trim_amount:]

            if not new_cached_items:
                return

            self.stats_needs_update = True

            try:
                tabs = self.query_one(TabbedContent)
                active_tab = tabs.active
                if active_tab in ["building_pane", "pa_pane", "ga_pane"]:
                    tree_id = f"#{active_tab.replace('_pane', '_tree')}"
                    tree = self.query_one(tree_id, Tree)
                    self._update_tree_labels_recursively(tree.root)
            except Exception as e:
                logging.error(f"Fehler beim Live-Update des Baums: {e}")

            has_ga_filter = bool(self.selected_gas)
            has_named_regex_filter = bool(self.active_named_regex_rules)
            has_global_regex_filter = bool(self.regex_filter)
            has_any_or_filter = has_ga_filter or has_named_regex_filter
            rows_to_add = []

            for item in new_cached_items:
                show_line = not has_any_or_filter
                if has_any_or_filter:
                    if has_ga_filter and item["ga"] in self.selected_gas:
                        show_line = True
                    elif has_named_regex_filter and not show_line:
                        for rule in self.active_named_regex_rules:
                            if rule.search(item["search_string"]):
                                show_line = True
                                break
                if not show_line: continue 
                
                if has_global_regex_filter:
                    if not self.regex_filter.search(item["search_string"]):
                        continue 
                
                payload = self._truncate_payload(item["payload"])
                        
                rows_to_add.append((
                    item["timestamp"], item["pa"], item["pa_name"],
                    item["ga"], item["ga_name"], payload
                ))
            
            if not rows_to_add: return 

            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            total_rows = self.log_widget.row_count + len(rows_to_add)
            
            if not has_any_or_filter and not has_global_regex_filter and total_rows > self.max_log_lines + 1000:
                self.log_view_is_dirty = True
                self._refilter_log_view() 
            else:
                self.log_widget.add_rows(rows_to_add)
                if is_at_bottom:
                    self.log_widget.scroll_end(animate=False, duration=0.0)
                
                # Update caption after adding rows
                if self.log_caption_label:
                    total_displayed = self.log_widget.row_count
                    self.log_caption_label.update(f"{total_displayed} entries shown.")
            
        except Exception as e:
            logging.error(f"Fehler im efficient_log_tail: {e}", exc_info=True)
            self.action_toggle_log_reload(force_off=True)

    def _refilter_log_view(self) -> None:
        if not self.log_widget: return
        self._process_log_lines()
        self.log_view_is_dirty = False
    
    # --- BAUM-LOGIK (TREES) ---
    
    def _populate_tree_from_data(self, tree: Tree, data: TreeData, expand_all: bool = False):
        tree.clear()
        def natural_sort_key(item: Tuple[str, Any]):
            key_str = str(item[0])
            return [int(c) if c.isdecimal() else c.lower() for c in re.split('([0-9]+)', key_str)]
        def add_nodes(parent_node: TreeNode, children_data: Dict[str, TreeData]):
            for _, node_data in sorted(children_data.items(), key=natural_sort_key):
                label = node_data.get("name")
                if not label: continue
                child_node = parent_node.add(label, data=node_data.get("data"))
                if node_children := node_data.get("children"):
                    add_nodes(child_node, node_children)
        
        if data and "children" in data:
            add_nodes(tree.root, data["children"])
        tree.root.collapse_all()
        if expand_all:
            tree.root.expand_all()

    def _get_descendant_gas(self, node: TreeNode) -> Set[str]:
        gas = set()
        if isinstance(node.data, dict) and "gas" in node.data:
            gas.update(node.data["gas"])
        for child in node.children:
            gas.update(self._get_descendant_gas(child))
        return gas

    def _get_node_payload_display(self, node_gas: set) -> Optional[str]:
        """
        Extract and format payload display from payload_history.
        Returns formatted Rich markup string with current and previous values, or None.
        """
        if not node_gas:
            return None
        
        combined_history = []
        for ga in node_gas:
            if ga in self.payload_history:
                combined_history.extend(self.payload_history[ga])
        
        if not combined_history:
            return None
        
        combined_history.sort(key=lambda x: x['timestamp'])
        latest_payloads = [item['payload'] for item in combined_history[-3:]]
        current_payload = latest_payloads[-1]
        previous_payloads = latest_payloads[-2::-1]
        
        payload_str = f"[bold yellow]{current_payload}[/]"
        if previous_payloads:
            history_str = ", ".join(previous_payloads)
            payload_str += f" [dim]({history_str})[/dim]"
        
        return payload_str

    def _update_parent_prefixes_recursive(self, node: Optional[TreeNode]) -> None:
        if not node or not node.parent:
            return
        
        display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))
        
        prefix = "[ ] "
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            elif selected_descendant_gas: 
                prefix = "[-] "
        
        node.set_label(prefix + display_label)
        if node.parent:
            self._update_parent_prefixes_recursive(node.parent)

    def _update_node_and_children_prefixes(self, node: TreeNode) -> None:
        display_label = ""
        
        if isinstance(node.data, dict) and "original_name" in node.data:
            original_name = node.data["original_name"]
            display_label = original_name
            
            # Regenerate payload display from payload_history instead of extracting from label
            node_gas = node.data.get("gas", set())
            payload_display = self._get_node_payload_display(node_gas)
            if payload_display:
                display_label = f"{original_name} -> {payload_display}"
        else:
            display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))

        prefix = "[ ] "
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            elif selected_descendant_gas: 
                prefix = "[-] "
        
        node.set_label(prefix + display_label)

        for child in node.children:
            self._update_node_and_children_prefixes(child)

    def _update_tree_labels_recursively(self, node: TreeNode) -> None:
        display_label = ""
        if isinstance(node.data, dict) and "original_name" in node.data:
            original_name = node.data["original_name"]
            display_label = original_name
            
            node_gas = node.data.get("gas", set())
            payload_display = self._get_node_payload_display(node_gas)
            if payload_display:
                display_label = f"{original_name} -> {payload_display}"
        else:
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
                    filtered_children[key] = filtered_child_data
            
            if has_matching_descendant:
                new_node_data = original_data.copy()
                new_node_data["children"] = filtered_children
                return new_node_data, True
        return None, False

    # --- NAMED FILTER LOGIK ---
    
    def _load_named_filters(self):
        self.named_filters.clear()
        self.named_filters_rules.clear()
        if not self.named_filter_path.exists():
            self._save_named_filters()
            return
        
        try:
            with open(self.named_filter_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)
                if not yaml_data: return
                self.named_filters = yaml_data
                for filter_name, rules_list in yaml_data.items():
                    if not isinstance(rules_list, list): continue
                    gas = set()
                    regex_patterns = []
                    for rule_str in rules_list:
                        rule_str = str(rule_str)
                        if re.fullmatch(r"^\d+/\d+/\d+$", rule_str):
                            gas.add(rule_str)
                        else:
                            try:
                                regex_patterns.append(re.compile(rule_str, re.IGNORECASE))
                            except re.error: pass
                    self.named_filters_rules[filter_name] = {"gas": gas, "regex": regex_patterns}
        except Exception as e:
            logging.error(f"Fehler beim Laden von {self.named_filter_path}: {e}")

    def _save_named_filters(self):
        try:
            ga_lookup = self.project_data.get("group_addresses", {})
            with open(self.named_filter_path, 'w', encoding='utf-8') as f:
                f.write("# KNX-Lens Named Selection Groups\n\n")
                for filter_name, rules_list in self.named_filters.items():
                    f.write(f"{filter_name}:\n")
                    if not rules_list:
                        f.write("  - \n")
                    else:
                        for rule_str in rules_list:
                            f.write(f"  - {rule_str}")
                            if re.fullmatch(r"^\d+/\d+/\d+$", rule_str):
                                name = ga_lookup.get(rule_str, {}).get("name", "N/A")
                                f.write(f" # {name}\n")
                            else:
                                f.write("\n")
                    f.write("\n")
        except Exception as e:
            logging.error(f"Fehler beim Speichern von {self.named_filter_path}: {e}")

    def _populate_named_filter_tree(self):
        tree = self.query_one("#named_filter_tree", Tree)
        tree.clear()
        tree_data_root = {"id": "filter_root", "name": "Selection Groups", "children": {}}
        for filter_name in sorted(self.named_filters.keys()):
            prefix = "[*] " if filter_name in self.active_named_filters else "[ ] "
            parent_node = tree.root.add(prefix + filter_name, data=filter_name)
            parent_data_node = {"id": f"filter_group_{filter_name}", "name": filter_name, "data": filter_name, "children": {}}
            rules_list = self.named_filters.get(filter_name)
            if rules_list:
                for rule_str in rules_list:
                    parent_node.add_leaf(rule_str, data=(filter_name, rule_str))
                    leaf_data = (filter_name, rule_str)
                    parent_data_node["children"][rule_str] = {"id": f"rule_{filter_name}_{rule_str}", "name": rule_str, "data": leaf_data, "children": {}}
            tree_data_root["children"][filter_name] = parent_data_node
        tree.root.expand()
        self.named_filters_tree_data = tree_data_root

    def _rebuild_active_regexes(self):
        self.active_named_regex_rules.clear()
        for filter_name in self.active_named_filters:
            if rules := self.named_filters_rules.get(filter_name):
                self.active_named_regex_rules.extend(rules["regex"])

    def _update_all_tree_prefixes(self):
        for tree_id in ("#building_tree", "#pa_tree", "#ga_tree"):
            try:
                tree = self.query_one(tree_id, Tree)
                self._update_node_and_children_prefixes(tree.root)
            except Exception: pass
        self._update_named_filter_prefixes()

    def _update_named_filter_prefixes(self):
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            for node in tree.root.children:
                if not isinstance(node.data, str): continue 
                filter_name = node.data
                prefix = "[*] " if filter_name in self.active_named_filters else "[ ] "
                display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))
                node.set_label(prefix + display_label)
        except Exception: pass

    # --- STATISTIK-LOGIK ---

    def _format_addr_label(self, identifier: str, display_name: str) -> str:
        """Gibt eine kombinierte Darstellung zurück, aber ohne doppelte Wiederholung."""
        if display_name and display_name != identifier and display_name != "N/A":
            return f"{identifier} — {display_name}"
        return identifier

    def _parse_timestamp_to_epoch(self, ts_str: str) -> Optional[float]:
        """Parst Timestamp zu Epoch-Sekunden; tolerant gegenüber ISO mit/ohne Datum."""
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            try:
                base_time = datetime.strptime(ts_str.split('.')[0], "%H:%M:%S").time()
                today = datetime.today().date()
                return datetime.combine(today, base_time).timestamp()
            except Exception:
                return None

    def _estimate_cycle_seconds(self, epoch_list: List[float]) -> Optional[float]:
        """Schätzt Wiederholintervall aus aufeinanderfolgenden Sendezeiten."""
        if not epoch_list or len(epoch_list) < 25:
            return None
        times = sorted(epoch_list)
        deltas = [round(times[i] - times[i-1]) for i in range(1, len(times)) if times[i] > times[i-1]]
        if len(deltas) < 24:
            return None
        freq: Dict[float, int] = {}
        for d in deltas:
            freq[d] = freq.get(d, 0) + 1
        mode_delta = max(freq.items(), key=lambda x: x[1])
        if mode_delta[1] >= 24 and mode_delta[1] / len(deltas) >= 0.5:
            return float(mode_delta[0])
        return None

    def _load_ga_size_map(self) -> Dict[str, int]:
        """
        Erstellt eine Map: GA-String -> Telegramm-Gesamtgröße (Bytes).
        Nutzt DPT-Informationen aus dem geladenen Projekt.
        """
        ga_size_map = {}
        
        if not self.project_data:
            return ga_size_map
        
        # Wrapper entpacken
        actual_data = self.project_data.get("project", self.project_data)
        
        if "group_addresses" not in actual_data:
            return ga_size_map
        
        for ga_str, ga_data in actual_data["group_addresses"].items():
            dpt_main = None
            
            if ga_data.get("dpt"):
                dpt_full = str(ga_data["dpt"])
                try:
                    dpt_main = int(dpt_full.split('.')[0])
                except ValueError:
                    pass
            
            payload_size = DEFAULT_PAYLOAD_SIZE
            if dpt_main in DPT_SIZE_MAP:
                payload_size = DPT_SIZE_MAP[dpt_main]
            
            ga_size_map[ga_str] = FRAME_OVERHEAD + payload_size
        
        return ga_size_map

    def _build_statistics_tree_data_pa_ga(self) -> TreeData:
        """
        Baut Statistik-Baum: PA (Parent) -> GA (Child) mit Bytes und Count.
        Sortiert nach Bytes (absteigend).
        """
        if not self.cached_log_data:
            return {}
        
        ga_sizes = self._load_ga_size_map()
        default_size = DEFAULT_FRAME_SIZE
        
        # Aggregation: (PA, GA) -> {count, bytes} + Timestamps
        stats: Dict[Tuple[str, str], Dict[str, int]] = {}
        times_by_key: Dict[Tuple[str, str], List[float]] = {}
        for log_entry in self.cached_log_data:
            pa = log_entry.get("pa", "unknown")
            ga = log_entry.get("ga", "unknown")
            key = (pa, ga)
            ts_epoch = self._parse_timestamp_to_epoch(log_entry.get("timestamp", ""))
            
            if key not in stats:
                stats[key] = {"count": 0, "bytes": 0}
            if ts_epoch is not None:
                times_by_key.setdefault(key, []).append(ts_epoch)

            stats[key]["count"] += 1
            frame_size = ga_sizes.get(ga, default_size)
            stats[key]["bytes"] += frame_size
        
        # Sortieren nach Bytes (absteigend)
        sorted_stats = sorted(stats.items(), key=lambda x: x[1]["bytes"], reverse=True)
        
        # Wrapper entpacken
        actual_data = self.project_data.get("project", self.project_data)
        devices_dict = actual_data.get("devices", {})
        ga_dict = actual_data.get("group_addresses", {})
        
        # Baum aufbauen: PA -> {GAs} mit Stats
        tree_data: Dict[str, Any] = {}
        pa_totals: Dict[str, Dict[str, int]] = {}
        
        for (pa, ga), stat_data in sorted_stats:
            if pa not in tree_data:
                pa_name = devices_dict.get(pa, {}).get("name", pa)
                tree_data[pa] = {
                    "children": {},
                    "gas": [],
                    "name": self._format_addr_label(pa, pa_name),
                    "node_id": pa,
                    "bytes": 0,
                    "count": 0
                }
                pa_totals[pa] = {"bytes": 0, "count": 0}
            
            pa_totals[pa]["bytes"] += stat_data["bytes"]
            pa_totals[pa]["count"] += stat_data["count"]
            
            # GA als Child
            ga_name = ga_dict.get(ga, {}).get("name", ga)
            cycle = self._estimate_cycle_seconds(times_by_key.get((pa, ga), []))
            tree_data[pa]["children"][ga] = {
                "name": self._format_addr_label(ga, ga_name),
                "node_id": ga,
                "bytes": stat_data["bytes"],
                "count": stat_data["count"],
                "percent": 0.0,
                "cycle_seconds": cycle
            }
        
        # Total Bytes berechnen
        total_bytes = sum(pa_totals[pa]["bytes"] for pa in pa_totals)
        
        # Prozentsätze und Labels anpassen
        for pa in tree_data:
            tree_data[pa]["bytes"] = pa_totals[pa]["bytes"]
            tree_data[pa]["count"] = pa_totals[pa]["count"]
            tree_data[pa]["percent"] = (tree_data[pa]["bytes"] / total_bytes * 100) if total_bytes > 0 else 0
            
            for ga in tree_data[pa]["children"]:
                tree_data[pa]["children"][ga]["percent"] = (
                    tree_data[pa]["children"][ga]["bytes"] / total_bytes * 100
                ) if total_bytes > 0 else 0
        
        return tree_data

    def _build_statistics_tree_data_ga_pa(self) -> TreeData:
        """
        Baut Statistik-Baum: GA (Parent) -> PA (Child) mit Bytes und Count.
        Sortiert nach Bytes (absteigend).
        """
        if not self.cached_log_data:
            return {}
        
        ga_sizes = self._load_ga_size_map()
        default_size = DEFAULT_FRAME_SIZE
        
        # Aggregation: (GA, PA) -> {count, bytes} + Timestamps
        stats: Dict[Tuple[str, str], Dict[str, int]] = {}
        times_by_key: Dict[Tuple[str, str], List[float]] = {}
        for log_entry in self.cached_log_data:
            pa = log_entry.get("pa", "unknown")
            ga = log_entry.get("ga", "unknown")
            key = (ga, pa)
            ts_epoch = self._parse_timestamp_to_epoch(log_entry.get("timestamp", ""))
            
            if key not in stats:
                stats[key] = {"count": 0, "bytes": 0}
            if ts_epoch is not None:
                times_by_key.setdefault(key, []).append(ts_epoch)

            stats[key]["count"] += 1
            frame_size = ga_sizes.get(ga, default_size)
            stats[key]["bytes"] += frame_size
        
        # Sortieren nach Bytes (absteigend)
        sorted_stats = sorted(stats.items(), key=lambda x: x[1]["bytes"], reverse=True)
        
        # Wrapper entpacken
        actual_data = self.project_data.get("project", self.project_data)
        devices_dict = actual_data.get("devices", {})
        ga_dict = actual_data.get("group_addresses", {})
        
        # Baum aufbauen: GA -> {PAs} mit Stats
        tree_data: Dict[str, Any] = {}
        ga_totals: Dict[str, Dict[str, int]] = {}
        
        for (ga, pa), stat_data in sorted_stats:
            if ga not in tree_data:
                ga_name = ga_dict.get(ga, {}).get("name", ga)
                tree_data[ga] = {
                    "children": {},
                    "name": self._format_addr_label(ga, ga_name),
                    "node_id": ga,
                    "bytes": 0,
                    "count": 0
                }
                ga_totals[ga] = {"bytes": 0, "count": 0}
            
            ga_totals[ga]["bytes"] += stat_data["bytes"]
            ga_totals[ga]["count"] += stat_data["count"]
            
            # PA als Child
            pa_name = devices_dict.get(pa, {}).get("name", pa)
            cycle = self._estimate_cycle_seconds(times_by_key.get((ga, pa), []))
            tree_data[ga]["children"][pa] = {
                "name": self._format_addr_label(pa, pa_name),
                "node_id": pa,
                "bytes": stat_data["bytes"],
                "count": stat_data["count"],
                "percent": 0.0,
                "cycle_seconds": cycle
            }
        
        # Total Bytes berechnen
        total_bytes = sum(ga_totals[ga]["bytes"] for ga in ga_totals)
        
        # Prozentsätze und Labels anpassen
        for ga in tree_data:
            tree_data[ga]["bytes"] = ga_totals[ga]["bytes"]
            tree_data[ga]["count"] = ga_totals[ga]["count"]
            tree_data[ga]["percent"] = (tree_data[ga]["bytes"] / total_bytes * 100) if total_bytes > 0 else 0
            
            for pa in tree_data[ga]["children"]:
                tree_data[ga]["children"][pa]["percent"] = (
                    tree_data[ga]["children"][pa]["bytes"] / total_bytes * 100
                ) if total_bytes > 0 else 0
        
        return tree_data

    def _populate_statistics_tree(self, tree: Tree, tree_data: TreeData, parent_node: Optional[TreeNode] = None) -> None:
        """
        Populiert einen Statistik-Baum mit Anteil und optionalem Sendetakt.
        """
        target_root = parent_node or tree.root
        
        for parent_key, parent_data in sorted(
            tree_data.items(), 
            key=lambda x: x[1].get("bytes", 0), 
            reverse=True
        ):
            parent_percent = parent_data.get("percent", 0.0)
            
            parent_label = (
                f"{parent_data['name']} "
                f"[bold cyan]Share: {parent_percent:.2f}%[/]"
            )
            
            parent_node = target_root.add(parent_label, expand=False)
            parent_node.data = parent_key
            
            children = parent_data.get("children", {})
            for child_key, child_data in sorted(
                children.items(),
                key=lambda x: x[1].get("bytes", 0),
                reverse=True
            ):
                child_percent = child_data.get("percent", 0.0)
                cycle_seconds = child_data.get("cycle_seconds")
                
                child_label = (
                    f"{child_data['name']} "
                    f"[yellow]Share: {child_percent:.2f}%[/]"
                )
                if cycle_seconds is not None:
                    child_label += f" [green]Cycle: ~{int(round(cycle_seconds))}s[/]"
                
                child_node = parent_node.add(child_label)
                child_node.data = child_key

    def _build_statistics_tree_data_ga_hierarchy(self) -> TreeData:
        """
        Baut Statistik-Baum: GA-Hierarchie (Haupt/Mittel/Unter) mit Bytes und Count.
        Sortiert nach Bytes (absteigend).
        """
        if not self.cached_log_data:
            return {}
        
        ga_sizes = self._load_ga_size_map()
        default_size = DEFAULT_FRAME_SIZE
        
        # Wrapper entpacken
        actual_data = self.project_data.get("project", self.project_data)
        ga_dict = actual_data.get("group_addresses", {})
        
        # Aggregation: GA -> {count, bytes, timestamps}
        ga_stats: Dict[str, Dict[str, Any]] = {}
        times_by_ga: Dict[str, List[float]] = {}
        
        for log_entry in self.cached_log_data:
            ga = log_entry.get("ga", "unknown")
            ts_epoch = self._parse_timestamp_to_epoch(log_entry.get("timestamp", ""))
            
            if ga not in ga_stats:
                ga_stats[ga] = {"count": 0, "bytes": 0}
            if ts_epoch is not None:
                times_by_ga.setdefault(ga, []).append(ts_epoch)
            
            ga_stats[ga]["count"] += 1
            frame_size = ga_sizes.get(ga, default_size)
            ga_stats[ga]["bytes"] += frame_size
        
        # Hierarchie aufbauen: Haupt -> Mittel -> Unter
        tree_data: Dict[str, Any] = {}
        total_bytes = sum(ga_stats[ga]["bytes"] for ga in ga_stats)
        
        for ga, stat_data in ga_stats.items():
            parts = ga.split('/')
            if len(parts) != 3:
                continue
            
            main_group, middle_group, sub_group = parts
            
            # Hauptgruppe
            if main_group not in tree_data:
                tree_data[main_group] = {
                    "children": {},
                    "name": f"Hauptgruppe {main_group}",
                    "node_id": main_group,
                    "bytes": 0,
                    "count": 0,
                    "percent": 0.0
                }
            
            tree_data[main_group]["bytes"] += stat_data["bytes"]
            tree_data[main_group]["count"] += stat_data["count"]
            
            # Mittelgruppe
            middle_key = f"{main_group}/{middle_group}"
            if middle_key not in tree_data[main_group]["children"]:
                tree_data[main_group]["children"][middle_key] = {
                    "children": {},
                    "name": f"Mittelgruppe {middle_key}",
                    "node_id": middle_key,
                    "bytes": 0,
                    "count": 0,
                    "percent": 0.0
                }
            
            tree_data[main_group]["children"][middle_key]["bytes"] += stat_data["bytes"]
            tree_data[main_group]["children"][middle_key]["count"] += stat_data["count"]
            
            # Untergruppe (finale GA)
            ga_name = ga_dict.get(ga, {}).get("name", ga)
            cycle = self._estimate_cycle_seconds(times_by_ga.get(ga, []))
            
            tree_data[main_group]["children"][middle_key]["children"][ga] = {
                "name": self._format_addr_label(ga, ga_name),
                "node_id": ga,
                "bytes": stat_data["bytes"],
                "count": stat_data["count"],
                "percent": (stat_data["bytes"] / total_bytes * 100) if total_bytes > 0 else 0,
                "cycle_seconds": cycle
            }
        
        # Prozentsätze für Haupt- und Mittelgruppen berechnen
        for main_group in tree_data:
            tree_data[main_group]["percent"] = (
                tree_data[main_group]["bytes"] / total_bytes * 100
            ) if total_bytes > 0 else 0
            
            for middle_key in tree_data[main_group]["children"]:
                tree_data[main_group]["children"][middle_key]["percent"] = (
                    tree_data[main_group]["children"][middle_key]["bytes"] / total_bytes * 100
                ) if total_bytes > 0 else 0
        
        return tree_data

    def _populate_ga_hierarchy_tree(self, tree: Tree, tree_data: TreeData, parent_node: Optional[TreeNode] = None) -> None:
        """
        Populiert einen hierarchischen GA-Baum (Haupt -> Mittel -> Unter).
        """
        target_root = parent_node or tree.root
        
        for main_key, main_data in sorted(
            tree_data.items(),
            key=lambda x: x[1].get("bytes", 0),
            reverse=True
        ):
            main_percent = main_data.get("percent", 0.0)
            main_label = f"{main_data['name']} [bold cyan]Share: {main_percent:.2f}%[/]"
            main_node = target_root.add(main_label, expand=False)
            main_node.data = main_key
            
            for middle_key, middle_data in sorted(
                main_data.get("children", {}).items(),
                key=lambda x: x[1].get("bytes", 0),
                reverse=True
            ):
                middle_percent = middle_data.get("percent", 0.0)
                middle_label = f"{middle_data['name']} [bold magenta]Share: {middle_percent:.2f}%[/]"
                middle_node = main_node.add(middle_label, expand=False)
                middle_node.data = middle_key
                
                for ga_key, ga_data in sorted(
                    middle_data.get("children", {}).items(),
                    key=lambda x: x[1].get("bytes", 0),
                    reverse=True
                ):
                    ga_percent = ga_data.get("percent", 0.0)
                    cycle_seconds = ga_data.get("cycle_seconds")
                    
                    ga_label = f"{ga_data['name']} [yellow]Share: {ga_percent:.2f}%[/]"
                    if cycle_seconds is not None:
                        ga_label += f" [green]Cycle: ~{int(round(cycle_seconds))}s[/]"
                    
                    ga_node = middle_node.add(ga_label)
                    ga_node.data = ga_key

    def _populate_statistics_combined(self, tree: Tree, pa_ga_data: TreeData, ga_pa_data: TreeData) -> None:
        """Ein Tree mit drei Hauptknoten: GA→PA, PA→GA und GA-Hierarchie."""
        tree.clear()
        root = tree.root
        
        ga_root = root.add("GA → PA", expand=False)
        self._populate_statistics_tree(tree, ga_pa_data, parent_node=ga_root)
        
        pa_root = root.add("PA → GA", expand=False)
        self._populate_statistics_tree(tree, pa_ga_data, parent_node=pa_root)
        
        # Dritter Knoten: GA-Hierarchie
        ga_hierarchy_data = self._build_statistics_tree_data_ga_hierarchy()
        hierarchy_root = root.add("GA Tree (Hierarchy)", expand=False)
        self._populate_ga_hierarchy_tree(tree, ga_hierarchy_data, parent_node=hierarchy_root)

