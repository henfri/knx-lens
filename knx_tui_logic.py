#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Logik-Mixin für KNX-Lens.
Enthält alle Helferfunktionen für Datenverarbeitung, Filtern und UI-Updates.
Wird von knx-lens.py importiert und als Basisklasse verwendet.
"""

import logging
import os
import re
import time
import zipfile
import io
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime, time as datetime_time

from textual.widgets import Tree, DataTable
from textual.widgets.tree import TreeNode

from knx_log_utils import parse_and_cache_log_data, append_new_log_lines

# --- Konstanten aus der Haupt-App ---
NAMED_FILTER_FILENAME = "named_filters.yaml" 
TreeData = Dict[str, Any] # <-- Hier ist der fehlende Import

class KNXTuiLogic:
    """
    Diese Klasse enthält die gesamte "Business-Logik" der App.
    Sie wird als Mixin (Basisklasse) in der Haupt-App KNXLens verwendet,
    damit sie auf 'self' (den App-Zustand) zugreifen kann.
    """

    # --- DATEN-LADE-LOGIK ---

    def _load_log_file_data_only(self) -> Tuple[bool, Optional[Exception]]:
        """
        [SYNCHRON]
        Liest die Log-Datei (vollständig) von der Festplatte und parst sie
        in `self.cached_log_data`. Führt KEINE UI-Aktionen aus.
        Gibt (is_zip, Exception) zurück.
        """
        log_file_path = self.config.get("log_file") or os.path.join(self.config.get("log_path", "."), "knx_bus.log")

        self.last_log_mtime = None
        self.last_log_position = 0

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
                    with zf.open(log_files_in_zip[0]) as log_file:
                        lines = io.TextIOWrapper(log_file, encoding='utf-8').readlines()
            else:
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    self.last_log_position = f.tell()
                self.last_log_mtime = os.path.getmtime(log_file_path)
            
            logging.debug("Starte parse_and_cache_log_data...")
            self.payload_history, self.cached_log_data = parse_and_cache_log_data(
                lines, 
                self.project_data,
                self.time_filter_start,
                self.time_filter_end
            )
            logging.debug("Beende parse_and_cache_log_data.")
            
            duration = time.time() - start_time
            logging.info(f"Log-Datei '{os.path.basename(log_file_path)}' in {duration:.2f}s gelesen und verarbeitet.")
            return is_zip, None

        except Exception as e:
            logging.error(f"Fehler beim Verarbeiten von '{log_file_path}': {e}", exc_info=True)
            self.cached_log_data = []
            self.payload_history.clear()
            return is_zip, e

    def _reload_log_file_sync(self):
        """
        [SYNCHRON]
        Wird aufgerufen, wenn der Benutzer eine neue Datei auswählt oder 'r' drückt.
        Friert die UI während des Ladens ein.
        """
        self._reset_user_activity() 
        logging.debug("_reload_log_file_sync: Starte synchrones Neuladen...")
        
        # 1. Daten laden (blockiert)
        reload_start_time = time.time()
        is_zip, error = self._load_log_file_data_only()
        
        # 2. UI-Update ausführen (blockiert)
        if error:
            if isinstance(error, FileNotFoundError):
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]FEHLER: {error}[/red]")
            else:
                self.log_widget.clear()
                self.log_widget.add_row(f"\n[red]Fehler beim Verarbeiten der Log-Datei: {error}[/red]")
                self.log_widget.add_row(f"[dim]{traceback.format_exc()}[/dim]")
            return

        logging.info("Log-Daten neu geladen. Aktualisiere UI-Bäume und Log-Ansicht...")
        start_time = time.time()
        
        logging.debug("Aktualisiere Baum-Labels...")
        for tree in self.query(Tree):
            if tree.id == "file_browser" or tree.id == "named_filter_tree": continue
            self._update_tree_labels_recursively(tree.root)
        logging.debug("Baum-Labels aktualisiert.")

        logging.debug("Starte _process_log_lines (initiale Log-Ansicht)...")
        self.paging_warning_shown = False # <-- POPUP-FLAG RESET
        self.log_view_is_dirty = True
        self._process_log_lines()
        self.log_view_is_dirty = False 
        logging.debug("Beende _process_log_lines.")

        if is_zip:
            self.action_toggle_log_reload(force_off=True)
        else:
            self.action_toggle_log_reload(force_on=True)
        
        duration = time.time() - start_time
        logging.info(f"UI-Aktualisierung nach Log-Laden in {duration:.2f}s abgeschlossen.")
        logging.info(f"Gesamtes Neuladen (sync) dauerte {time.time() - reload_start_time:.4f}s")

    # --- LOG-TABELLEN-LOGIK ---

    def _process_log_lines(self):
        """
        [SYNCHRON]
        Filtert die in `self.cached_log_data` zwischengespeicherten,
        angereicherten Log-Einträge basierend auf `self.selected_gas`
        und füllt die `DataTable`.
        """
        if not self.log_widget: return
        
        try:
            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            
            self.log_widget.clear()
            # --- NEUE FILTER-LOGIK ---
            has_ga_filter = bool(self.selected_gas)
            has_named_regex_filter = bool(self.active_named_regex_rules)
            has_global_regex_filter = bool(self.regex_filter)
            has_any_or_filter = has_ga_filter or has_named_regex_filter
            # ---
    
            if not self.cached_log_data:
                 self.log_widget.add_row("[yellow]Keine Log-Daten geladen oder Log-Datei ist leer.[/yellow]")
                 self.log_widget.caption = "Keine Log-Daten"
                 return
            
            start_time = time.time()
            log_caption = ""
            log_entries_to_process = self.cached_log_data 
            
            if has_ga_filter:
                log_caption += f"GA-Filter ({len(self.selected_gas)})"
            if has_named_regex_filter:
                log_caption += f" | Regex-Filter ({len(self.active_named_regex_rules)})"
            if has_global_regex_filter:
                log_caption += f" | AND Grep ('{self.regex_filter_string}')"

            found_count = 0
            rows_to_add = []
            
            logging.debug("Starte Filter-Loop...")
            filter_start_time = time.time()
            
            for i, log_entry in enumerate(log_entries_to_process):
                
                # --- NEUE FILTER-LOGIK (NAMED FILTERS) ---
                show_line = not has_any_or_filter # Wenn kein OR-Filter aktiv ist, erstmal alle zeigen
                
                # 1. OR-Pool (GAs ODER Named Regexes)
                if has_any_or_filter:
                    if has_ga_filter and log_entry["ga"] in self.selected_gas:
                        show_line = True
                    elif has_named_regex_filter and not show_line: # Nur prüfen, wenn nicht schon durch GA-Match
                        for rule in self.active_named_regex_rules:
                            if rule.search(log_entry["search_string"]):
                                show_line = True
                                break
                
                if not show_line:
                    continue # Weder GA noch Named Regex haben gematcht
                
                # 2. Globaler AND-Pool (Der 'grep'-Input)
                if has_global_regex_filter:
                    if not self.regex_filter.search(log_entry["search_string"]):
                        continue # Hat OR-Pool gematcht, aber nicht den globalen AND-Regex
                # --- ENDE NEUE LOGIK ---

                # Zeile hat alle Filter bestanden
                rows_to_add.append((
                    log_entry["timestamp"],
                    log_entry["pa"],
                    log_entry["pa_name"],
                    log_entry["ga"],
                    log_entry["ga_name"],
                    log_entry["payload"]
                ))
            
            found_count = len(rows_to_add)

            filter_duration = time.time() - filter_start_time
            logging.debug(f"Filter-Loop beendet in {filter_duration:.4f}s. {found_count} Zeilen gefunden.")

            # --- PAGING-LOGIK (Limit GILT IMMER, nutzt self.max_log_lines) ---
            if found_count > self.max_log_lines:
                truncated_count = found_count - self.max_log_lines
                caption_text = f" | Zeige letzte {self.max_log_lines} von {found_count} Treffern"
                log_caption += caption_text
                logging.warning(f"Zu viele Zeilen ({found_count}). Zeige nur die letzten {self.max_log_lines}.")
                
                rows_to_add = rows_to_add[-self.max_log_lines:]
                
                if not self.paging_warning_shown:
                    self.notify(
                        f"Anzeige auf {self.max_log_lines} Zeilen begrenzt ({truncated_count} ältere ausgeblendet).",
                        title="Filter-Limit",
                        severity="warning",
                        timeout=10
                    )
                    self.paging_warning_shown = True 
                
            elif not has_any_or_filter and not has_global_regex_filter:
                log_caption = f"Alle Einträge ({found_count})"
            # --- ENDE PAGING-LOGIK ---

            logging.debug(f"Starte log_widget.add_rows({len(rows_to_add)} Zeilen)...")
            add_rows_start_time = time.time()

            self.log_widget.add_rows(rows_to_add)
            
            add_rows_duration = time.time() - add_rows_start_time
            logging.debug(f"log_widget.add_rows beendet in {add_rows_duration:.4f}s.")

            duration = time.time() - start_time
            logging.info(f"Log-Ansicht gefiltert. {len(rows_to_add)} Einträge in {duration:.4f}s gerendert.")
            self.log_widget.caption = f"{len(rows_to_add)} Einträge angezeigt. ({duration:.2f}s) | {log_caption}"

            if is_at_bottom:
                self.log_widget.scroll_end(animate=False, duration=0.0)

        except Exception as e:
            logging.error(f"Schwerer Fehler in _process_log_lines: {e}", exc_info=True)
            if self.log_widget:
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]Fehler beim Verarbeiten der Log-Zeilen: {e}[/red]")

    def _efficient_log_tail(self) -> None:
        """
        [TIMER]
        Prüft effizient auf Log-Änderungen ('tail -f'-Logik) 
        und parst nur neue Zeilen.
        """
        
        idle_duration = time.time() - self.last_user_activity
        if idle_duration > 3600:
            self.notify("Log Auto-Reload wegen Inaktivität pausiert.", title="Log Ansicht")
            logging.info("Auto-Reload wegen Inaktivität (1h) pausiert.")
            self.action_toggle_log_reload(force_off=True) 
            return 

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

            # --- NEUE FILTER-LOGIK ---
            has_ga_filter = bool(self.selected_gas)
            has_named_regex_filter = bool(self.active_named_regex_rules)
            has_global_regex_filter = bool(self.regex_filter)
            has_any_or_filter = has_ga_filter or has_named_regex_filter
            # ---
            rows_to_add = []

            logging.debug(f"Filtere {len(new_cached_items)} neue Zeilen (OR-Filter: {has_any_or_filter}, AND-Filter: {has_global_regex_filter})...")
            
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
                
                if not show_line:
                    continue 
                
                if has_global_regex_filter:
                    if not self.regex_filter.search(item["search_string"]):
                        continue 
                        
                rows_to_add.append((
                    item["timestamp"], item["pa"], item["pa_name"],
                    item["ga"], item["ga_name"], item["payload"]
                ))
            
            logging.debug(f"{len(rows_to_add)} neue Zeilen passen zu den Filtern.")
            
            if not rows_to_add:
                return 

            is_at_bottom = self.log_widget.scroll_y >= self.log_widget.max_scroll_y
            
            # --- POPUP-SPAM-FIX: Check enthält jetzt Filter-Status ---
            total_rows = self.log_widget.row_count + len(rows_to_add)
            
            if not has_any_or_filter and not has_global_regex_filter and total_rows > self.max_log_lines:
                logging.info(f"Tailing (ohne Filter) überschreitet Anzeigelimit ({total_rows} > {self.max_log_lines}). Lade Ansicht neu...")
                self.log_view_is_dirty = True
                self._refilter_log_view() # Baut die Tabelle mit den letzten 10k neu auf
            else:
                # Sonst: Einfach hinzufügen (schnell und leise)
                self.log_widget.add_rows(rows_to_add)
                if is_at_bottom:
                    self.log_widget.scroll_end(animate=False, duration=0.0)
            # --- ENDE POPUP-FIX ---
            
        except FileNotFoundError:
            self.notify(f"Log-Datei '{log_file_path}' nicht mehr gefunden.", severity="error")
            self.action_toggle_log_reload(force_off=True)
        except Exception as e:
            logging.error(f"Fehler im efficient_log_tail: {e}", exc_info=True)
            self.notify(f"Fehler beim Log-Reload: {e}", severity="error")
            self.action_toggle_log_reload(force_off=True)

    def _refilter_log_view(self) -> None:
        """Wird aufgerufen, wenn Filter (GA/Regex) sich ändern."""
        if not self.log_widget: return
        logging.info("Log-Ansicht wird mit gecachten Daten neu gefiltert (synchron).")
        self._process_log_lines()
        self.log_view_is_dirty = False # Flag löschen nach dem Filtern
    
    # --- BAUM-LOGIK (TREES) ---
    
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
        
        logging.debug(f"_populate_tree_from_data: '{tree.id or 'unbekannt'}' Knoten hinzugefügt.")
        
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
        """
        [PERFORMANCE-FIX - UP]
        Aktualisiert rekursiv alle Eltern-Knoten (für [-] Status).
        """
        if not node or not node.parent: # Stop an der (sichtbaren) Wurzel
            return
        
        # Hole den reinen Label-Text (ohne altes Präfix)
        display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))
        
        # 1. Präfix bestimmen
        prefix = "[ ] "
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            elif selected_descendant_gas: 
                prefix = "[-] "
        
        # 2. Label setzen
        node.set_label(prefix + display_label)

        # 3. Rekursiv für Eltern aufrufen
        if node.parent:
            self._update_parent_prefixes_recursive(node.parent)

    def _update_node_and_children_prefixes(self, node: TreeNode) -> None:
        """
        [PERFORMANCE-FIX - DOWN]
        Aktualisiert rekursiv den Knoten selbst und alle Kinder.
        """
        display_label = ""
        
        # 1. Hole den reinen Label-Text
        if isinstance(node.data, dict) and "original_name" in node.data:
            # Dies ist ein Blatt-Knoten (CO, GA, etc.)
            display_label = node.data["original_name"]
            
            # Behalte den Payload bei, falls er schon existiert
            current_label = str(node.label)
            payload_match = re.search(r"->\s*(\[bold yellow\].*)", current_label)
            if payload_match:
                display_label = f"{display_label} -> {payload_match.group(1)}"
        else:
            # Dies ist ein Eltern-Knoten (z.B. "Keller" oder "Gebäude")
            display_label = re.sub(r"^(\[[ *\-]] )+", "", str(node.label))

        # 2. Präfix bestimmen
        prefix = "[ ] "
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            elif selected_descendant_gas: 
                prefix = "[-] "
        
        # 3. Label setzen
        node.set_label(prefix + display_label)

        # 4. Rekursiv für Kinder aufrufen
        for child in node.children:
            self._update_node_and_children_prefixes(child)

    def _update_tree_labels_recursively(self, node: TreeNode) -> None:
        """
        [LANGSAME FUNKTION]
        Lädt Payloads und aktualisiert den gesamten Baum.
        Nur beim Start/Reload aufrufen!
        """
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
            # Dies ist ein Eltern-Knoten (z.B. "Keller")
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
                    filtered_children[key] = child_data
            
            if has_matching_descendant:
                new_node_data = original_data.copy()
                new_node_data["children"] = filtered_children
                return new_node_data, True
        return None, False

    # --- NAMED FILTER LOGIK ---
    
    def _load_named_filters(self):
        """Lädt und kompiliert die named_filters.yaml."""
        self.named_filters.clear()
        self.named_filters_rules.clear()
        if not self.named_filter_path.exists():
            logging.warning(f"{NAMED_FILTER_FILENAME} nicht gefunden. Erstelle leere Datei.")
            self._save_named_filters() # Erstellt eine leere Datei
            return
        
        try:
            with open(self.named_filter_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)
                if not yaml_data: return
                
                self.named_filters = yaml_data
                
                # Kompiliere die Regeln
                for filter_name, rules_list in yaml_data.items():
                    if not isinstance(rules_list, list):
                        logging.warning(f"Filter '{filter_name}' hat ungültiges Format (keine Liste) und wird ignoriert.")
                        continue
                        
                    gas = set()
                    regex_patterns = []
                    for rule_str in rules_list:
                        rule_str = str(rule_str)
                        if re.fullmatch(r"^\d+/\d+/\d+$", rule_str):
                            gas.add(rule_str)
                        else:
                            try:
                                regex_patterns.append(re.compile(rule_str, re.IGNORECASE))
                            except re.error as e:
                                logging.warning(f"Ungültiger Regex '{rule_str}' in Filter '{filter_name}' ignoriert: {e}")
                    
                    self.named_filters_rules[filter_name] = {"gas": gas, "regex": regex_patterns}
            logging.info(f"{len(self.named_filters_rules)} Named Filters geladen.")
        except Exception as e:
            logging.error(f"Fehler beim Laden von {self.named_filter_path}: {e}")
            self.notify(f"Fehler beim Laden von {NAMED_FILTER_FILENAME}: {e}", severity="error")

    def _save_named_filters(self):
        """
        [WUNSCH 3]
        Speichert self.named_filters (die Roh-Strings) in die YAML-Datei,
        inklusive GA-Namen als Kommentare.
        """
        try:
            ga_lookup = self.project_data.get("group_addresses", {})
            
            with open(self.named_filter_path, 'w', encoding='utf-8') as f:
                # Schreibe Header
                f.write("# KNX-Lens Benannte Filter\n")
                f.write("# Format: Jede Zeile ist eine GA (z.B. 1/1/1) oder ein Regex (z.B. .*Licht.*)\n\n")
                
                for filter_name, rules_list in self.named_filters.items():
                    f.write(f"{filter_name}:\n")
                    if not rules_list:
                        f.write("  - \n") # Leere Liste
                    else:
                        for rule_str in rules_list:
                            f.write(f"  - {rule_str}")
                            
                            # Kommentar hinzufügen, wenn es eine GA ist
                            if re.fullmatch(r"^\d+/\d+/\d+$", rule_str):
                                name = ga_lookup.get(rule_str, {}).get("name", "N/A")
                                f.write(f" # {name}\n")
                            else:
                                f.write("\n") # Nur Zeilenumbruch für Regex
                    f.write("\n") # Leerzeile zwischen Filtern
                    
            logging.info(f"Named Filters gespeichert in {self.named_filter_path}")
        except Exception as e:
            logging.error(f"Fehler beim Speichern von {self.named_filter_path}: {e}")
            self.notify(f"Fehler beim Speichern der Filter: {e}", severity="error")

    def _populate_named_filter_tree(self):
        """
        [WUNSCH 1]
        Füllt den Baum der Filter-Gruppen, inklusive der Regeln als Kinder.
        """
        tree = self.query_one("#named_filter_tree", Tree)
        tree.clear()
        for filter_name in sorted(self.named_filters.keys()):
            prefix = "[*] " if filter_name in self.active_named_filters else "[ ] "
            # Füge den Filter-Namen als Eltern-Knoten hinzu
            parent_node = tree.root.add(prefix + filter_name, data=filter_name)
            
            # Füge die Regeln als Kind-Knoten hinzu
            rules_list = self.named_filters.get(filter_name)
            if rules_list:
                for rule_str in rules_list:
                    # Data-Format: (FilterName, RegelString)
                    parent_node.add_leaf(rule_str, data=(filter_name, rule_str))
        tree.root.expand()

    def _rebuild_active_regexes(self):
        """Baut den Pool der aktiven Regex-Regeln neu auf."""
        self.active_named_regex_rules.clear()
        for filter_name in self.active_named_filters:
            if rules := self.named_filters_rules.get(filter_name):
                self.active_named_regex_rules.extend(rules["regex"])

    def _update_all_tree_prefixes(self):
        """Aktualisiert alle Bäume, um den [*] Status zu synchronisieren."""
        logging.debug("Aktualisiere alle Baum-Präfixe...")
        
        # --- BUGFIX (WUNSCH 2) ---
        # Rief fälschlicherweise _update_parent_prefixes_recursive auf
        for tree_id in ("#building_tree", "#pa_tree", "#ga_tree"):
            try:
                tree = self.query_one(tree_id, Tree)
                # Korrekte Funktion: Gehe von oben nach unten
                self._update_node_and_children_prefixes(tree.root)
            except Exception as e:
                logging.warning(f"Konnte Präfixe für Baum {tree_id} nicht aktualisieren: {e}")
        # --- ENDE BUGFIX ---
        
        # Schnelles Update für Named-Filter-Baum
        self._update_named_filter_prefixes()
        logging.debug("Baum-Präfix-Update beendet.")

    def _update_named_filter_prefixes(self):
        """Aktualisiert nur die Präfixe im Named-Filter-Baum."""
        try:
            tree = self.query_one("#named_filter_tree", Tree)
            for node in tree.root.children:
                filter_name = node.data
                prefix = "[*] " if filter_name in self.active_named_filters else "[ ] "
                node.set_label(prefix + filter_name)
        except Exception as e:
            logging.debug(f"Fehler beim Update der Named-Filter-Präfixe: {e}")