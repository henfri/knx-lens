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

NAMED_FILTER_FILENAME = "named_filters.yaml" 
TreeData = Dict[str, Any]
MAX_CACHE_SIZE = 50000 

class KNXTuiLogic:
    """
    Diese Klasse enthält die gesamte "Business-Logik" der App.
    """

    # --- DATEN-LADE-LOGIK ---

    def _load_log_file_data_only(self) -> Tuple[bool, Optional[Exception]]:
        """
        [SYNCHRON]
        Liest die Log-Datei von der Festplatte.
        """
        log_file_path = self.config.get("log_file") or os.path.join(self.config.get("log_path", "."), "knx_bus.log")

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
                
                rows_to_add.append((
                    log_entry["timestamp"], log_entry["pa"], log_entry["pa_name"],
                    log_entry["ga"], log_entry["ga_name"], log_entry["payload"]
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
                        
                rows_to_add.append((
                    item["timestamp"], item["pa"], item["pa_name"],
                    item["ga"], item["ga_name"], item["payload"]
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
            display_label = node.data["original_name"]
            current_label = str(node.label)
            payload_match = re.search(r"->\s*(\[bold yellow\].*)", current_label)
            if payload_match:
                display_label = f"{display_label} -> {payload_match.group(1)}"
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