#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ein interaktiver KNX Projekt-Explorer und Log-Filter.
- Ermöglicht das Browsen des Projekts nach Gebäude-, Physikalischer und Gruppen-Struktur.
- Filtert Log-Dateien (auch aus .zip-Archiven) basierend auf der Auswahl.
- Zeigt den zuletzt empfangenen Payload und eine kurze Historie direkt im Baum an.
- Shortcuts: (o) Log öffnen, (r) Log neu laden, (f) Filtern, (a) Auswahl, (t) Auto-Reload, (c) Kopieren, (q) Beenden.

*** NEU in dieser Version ***
- Verwendet DataTable statt RichLog für die Log-Ansicht.
- Spalten 3 (Gerätename) und 5 (GA-Name) sind flexibel und füllen den verfügbaren Platz.
- Log-Zeilen werden beim Laden "angereichert", d.h. PA- und GA-Namen werden
  aus den Projektdaten nachgeschlagen und in einer gecachten Struktur gespeichert.
- Das Filtern der Log-Ansicht ist dadurch extrem schnell.
"""
import json
import csv
import argparse
import os
import hashlib
import sys
import traceback
import re
import zipfile
import io
import logging
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

# Third-party libraries
from dotenv import load_dotenv, set_key, find_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Center, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, Tree, Static, Input, TabbedContent, TabPane, Label, Button, DataTable
from textual.widgets.tree import TreeNode
from textual import events  # Für Resize
from textual.timer import Timer
from xknxproject import XKNXProj

### --- SETUP & KONSTANTEN ---
# Anpassbares Log-Level (Optionen: logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR)
LOG_LEVEL = logging.INFO
TreeData = Dict[str, Any]
MAX_LOG_LINES_NO_FILTER = 5000  # Performance-Fix

### --- KERNLOGIK: PARSING & DATENSTRUKTURIERUNG ---
# (Unverändert)

def get_md5_hash(file_path: str) -> str:
    """Berechnet den MD5-Hash einer Datei."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def load_or_parse_project(knxproj_path: str, password: Optional[str]) -> Dict:
    """Lädt ein KNX-Projekt aus dem Cache oder parst es neu."""
    start_time = time.time()
    project_data = {}
    if not os.path.exists(knxproj_path):
        raise FileNotFoundError(f"Projektdatei nicht gefunden unter '{knxproj_path}'")
    
    cache_path = knxproj_path + ".cache.json"
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8') as f:
            try:
                cache_data = json.load(f)
                current_md5 = get_md5_hash(knxproj_path)
                if cache_data.get("md5") == current_md5:
                    logging.info("Cache ist aktuell. Lade aus dem Cache...")
                    duration = time.time() - start_time
                    logging.info(f"Projekt '{os.path.basename(knxproj_path)}' in {duration:.2f}s aus dem Cache geladen.")
                    return cache_data["project"]
            except (json.JSONDecodeError, KeyError):
                logging.warning("Cache ist korrupt. Parse Projekt neu...")
    
    logging.info(f"Parse KNX-Projektdatei: {knxproj_path} (dies kann einen Moment dauern)...")
    xknxproj = XKNXProj(knxproj_path, password=password)
    project_data = xknxproj.parse()

    current_md5 = get_md5_hash(knxproj_path)
    new_cache_data = {"md5": current_md5, "project": project_data}
    logging.info(f"Speichere neuen Cache nach {cache_path}")
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(new_cache_data, f, indent=2)

    duration = time.time() - start_time
    logging.info(f"Projekt '{os.path.basename(knxproj_path)}' in {duration:.2f}s neu geparst und gecacht.")
    
    return project_data

def get_best_channel_name(channel: Dict, ch_id: str) -> str:
    return channel.get("function_text") or channel.get("name") or f"Kanal-{ch_id}"

def add_com_objects_to_node(parent_node: Dict, com_obj_ids: List[str], project_data: Dict):
    """Fügt Communication Objects als Kinder zu einem Knoten hinzu und speichert den Originalnamen."""
    comm_objects = project_data.get("communication_objects", {})
    for co_id in com_obj_ids:
        co = comm_objects.get(co_id)
        if co:
            co_name = co.get("name", f"CO-{co_id}")
            gas = co.get("group_address_links", [])
            gas_str = ", ".join(gas)
            co_label = f"{co['number']}: {co_name} → [{gas_str}]"
            parent_node["children"][co_label] = {
                "id": f"co_{co_id}", "name": co_label,
                "data": {"type": "co", "gas": set(gas), "original_name": co_label}, 
                "children": {}
            }

def build_ga_tree_data(project: Dict) -> TreeData:
    """
    Baut eine hierarchische Baumstruktur von Gruppenadressen aus einem KNX-Projekt.
    """
    group_addresses = project.get("group_addresses", {})
    group_ranges = project.get("group_ranges", {})
    root_node: TreeData = {"id": "ga_root", "name": "Funktionen", "children": {}}
    
    if not group_addresses:
        return root_node
        
    hierarchy: TreeData = {}

    for address in group_addresses.keys():
        parts = address.split('/')
        if len(parts) == 3:
            main_key, sub_key_part, _ = parts
            sub_key = f"{main_key}/{sub_key_part}"
            if main_key not in hierarchy:
                hierarchy[main_key] = {"name": "", "subgroups": {}}
            if sub_key not in hierarchy[main_key]["subgroups"]:
                hierarchy[main_key]["subgroups"][sub_key] = {"name": "", "addresses": {}}
            hierarchy[main_key]["subgroups"][sub_key]["addresses"][address] = {"name": ""}

    flat_group_ranges = {}
    def flatten_ranges(ranges_to_flatten: Dict):
        for addr, details in ranges_to_flatten.items():
            details_copy = details.copy()
            nested_ranges = details_copy.pop("group_ranges", None)
            flat_group_ranges[addr] = details_copy
            if nested_ranges:
                flatten_ranges(nested_ranges)

    flatten_ranges(group_ranges)

    for address, details in flat_group_ranges.items():
        parts = address.split('/')
        name = details.get("name")
        if not name: continue
        if len(parts) == 1:
            main_key = parts[0]
            if main_key in hierarchy:
                hierarchy[main_key]["name"] = name
        elif len(parts) == 2:
            main_key, _ = parts
            if main_key in hierarchy and address in hierarchy[main_key].get("subgroups", {}):
                hierarchy[main_key]["subgroups"][address]["name"] = name

    for address, details in group_addresses.items():
        parts = address.split('/')
        name = details.get("name")
        if len(parts) == 3 and name:
            main_key, sub_key_part, _ = parts
            sub_key = f"{main_key}/{sub_key_part}"
            if main_key in hierarchy and sub_key in hierarchy[main_key].get("subgroups", {}):
                if address in hierarchy[main_key]["subgroups"][sub_key].get("addresses", {}):
                    hierarchy[main_key]["subgroups"][sub_key]["addresses"][address]["name"] = name

    sorted_main_keys = sorted(hierarchy.keys(), key=int)
    for main_key in sorted_main_keys:
        main_group = hierarchy[main_key]
        main_node_name = f"({main_key}) {main_group.get('name') or f'HG {main_key}'}"
        main_node = root_node["children"].setdefault(main_key, {"id": f"ga_main_{main_key}", "name": main_node_name, "children": {}})

        sorted_sub_keys = sorted(main_group.get("subgroups", {}).keys(), key=lambda k: [int(p) for p in k.split('/')])
        for sub_key in sorted_sub_keys:
            sub_group = main_group["subgroups"][sub_key]
            sub_node_name = f"({sub_key}) {sub_group.get('name') or f'MG {sub_key}'}"
            sub_node = main_node["children"].setdefault(sub_key, {"id": f"ga_sub_{sub_key.replace('/', '_')}", "name": sub_node_name, "children": {}})

            sorted_addresses = sorted(sub_group.get("addresses", {}).items(), key=lambda item: [int(p) for p in item[0].split('/')])
            for addr_str, addr_details in sorted_addresses:
                leaf_name = f"({addr_str}) {addr_details.get('name') or 'N/A'}"
                sub_node["children"][addr_str] = {
                    "id": f"ga_{addr_str}", "name": leaf_name, 
                    "data": {"type": "ga", "gas": {addr_str}, "original_name": leaf_name}, 
                    "children": {}
                }
    
    return root_node

def build_pa_tree_data(project: Dict) -> TreeData:
    pa_tree = {"id": "pa_root", "name": "Physikalische Adressen", "children": {}}
    devices = project.get("devices", {})
    topology = project.get("topology", {})
    
    area_names = {str(area['address']): area.get('name', '') for area in topology.get("areas", {}).values()}
    line_names = {}
    for area in topology.get("areas", {}).values():
        for line in area.get("lines", {}).values():
            line_id = f"{area['address']}.{line['address']}"
            line_names[line_id] = line.get('name', '')

    for pa, device in devices.items():
        parts = pa.split('.')
        if len(parts) != 3:
            logging.warning(f"Skipping malformed PA: {pa}")
            continue
        
        area_id, line_id_part, dev_id = parts
        line_id = f"{area_id}.{line_id_part}"

        area_name = area_names.get(area_id)
        area_label = f"({area_id}) {area_name}" if area_name and area_name != f"Bereich {area_id}" else f"Bereich {area_id}"
        area_node = pa_tree["children"].setdefault(area_id, {"id": f"pa_{area_id}", "name": area_label, "children": {}})

        line_name = line_names.get(line_id)
        line_label = f"({line_id}) {line_name}" if line_name and line_name != f"Linie {line_id}" else f"Linie {line_id}"
        line_node = area_node["children"].setdefault(line_id_part, {"id": f"pa_{line_id}", "name": line_label, "children": {}})

        device_name = f"({pa}) {device.get('name', 'N/A')}"
        device_node = line_node["children"].setdefault(dev_id, {"id": f"dev_{pa}", "name": device_name, "children": {}})
        
        processed_co_ids = set()
        for ch_id, channel in device.get("channels", {}).items():
            ch_name = get_best_channel_name(channel, ch_id)
            ch_node = device_node["children"].setdefault(ch_name, {"id": f"ch_{pa}_{ch_id}", "name": ch_name, "children": {}})
            co_ids_in_channel = channel.get("communication_object_ids", [])
            add_com_objects_to_node(ch_node, co_ids_in_channel, project)
            processed_co_ids.update(co_ids_in_channel)
        
        all_co_ids = set(device.get("communication_object_ids", []))
        device_level_co_ids = all_co_ids - processed_co_ids
        if device_level_co_ids:
            add_com_objects_to_node(device_node, list(device_level_co_ids), project)
    
    return pa_tree

def build_building_tree_data(project: Dict) -> TreeData:
    building_tree = {"id": "bldg_root", "name": "Gebäudestruktur", "children": {}}
    locations = project.get("locations", {})
    devices = project.get("devices", {})
    def process_space(space: Dict, parent_node: Dict):
        space_name = space.get("name", "Unbenannter Bereich")
        space_node = parent_node["children"].setdefault(space_name, {"id": f"loc_{space.get('identifier', space_name)}", "name": space_name, "children": {}})
        for pa in space.get("devices", []):
            device = devices.get(pa)
            if not device: continue
            device_name = f"({pa}) {device.get('name', 'Unbenannt')}"
            device_node = space_node["children"].setdefault(device_name, {"id": f"dev_{pa}", "name": device_name, "children": {}})
            processed_co_ids = set()
            for ch_id, channel in device.get("channels", {}).items():
                ch_name = get_best_channel_name(channel, ch_id)
                ch_node = device_node["children"].setdefault(ch_name, {"id": f"ch_{pa}_{ch_id}", "name": ch_name, "children": {}})
                co_ids_in_channel = channel.get("communication_object_ids", [])
                add_com_objects_to_node(ch_node, co_ids_in_channel, project)
                processed_co_ids.update(co_ids_in_channel)
            all_co_ids = set(device.get("communication_object_ids", []))
            device_level_co_ids = all_co_ids - processed_co_ids
            if device_level_co_ids:
                add_com_objects_to_node(device_node, list(device_level_co_ids), project)
        for child_space in space.get("spaces", {}).values():
            process_space(child_space, space_node)
    for location in locations.values():
        process_space(location, building_tree)
    return building_tree

### --- TUI: SCREENS & MODALS ---
# (Unverändert)

class FilterInputScreen(ModalScreen[str]):
    """Ein modaler Bildschirm für die Filtereingabe."""
    def compose(self) -> ComposeResult:
        yield Center(Vertical(
            Label("Baum filtern (Enter zum Bestätigen, ESC zum Abbrechen):"),
            Input(placeholder="Filtertext...", id="filter_input"),
            id="filter_dialog"
        ))
    def on_mount(self) -> None: self.query_one("#filter_input", Input).focus()
    def on_input_submitted(self, event: Input.Submitted) -> None: self.dismiss(event.value)
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape": self.dismiss("")

class OpenFileScreen(ModalScreen[Tuple[str, bool]]):
    """Ein modaler Bildschirm zum Öffnen einer Log-Datei."""
    def compose(self) -> ComposeResult:
        yield Center(Vertical(
            Label("Pfad zur Log-Datei (.log oder .zip) eingeben:"),
            Input(placeholder="/pfad/zur/datei.log", id="path_input"),
            Horizontal(
                Button("Temporär öffnen", variant="primary", id="open_temp"),
                Button("Öffnen & als Standard speichern", variant="success", id="open_save"),
                Button("Abbrechen", variant="error", id="cancel"),
            ),
            id="open_file_dialog"
        ))
    def on_mount(self) -> None: self.query_one("#path_input", Input).focus()
    def on_button_pressed(self, event: Button.Pressed) -> None:
        path = self.query_one(Input).value
        if event.button.id == "cancel" or not path:
            self.dismiss(("", False))
        elif event.button.id == "open_temp":
            self.dismiss((path, False))
        elif event.button.id == "open_save":
            self.dismiss((path, True))

### --- TUI: HAUPTANWENDUNG ---
class KNXLens(App):
    CSS_PATH = "knx-lens.css"
    BINDINGS = [
        Binding("q", "quit", "Beenden"),
        Binding("a", "toggle_selection", "Auswahl"),
        Binding("c", "copy_label", "Kopieren"),
        Binding("f", "filter_tree", "Filtern"),
        Binding("o", "open_log_file", "Log öffnen"),
        Binding("r", "reload_log_file", "Log neu laden"),
        Binding("t", "toggle_log_reload", "Auto-Reload Log"),
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
    
    # --- LAYOUT-FIX (V8) + DEBUG-LOGGING ---
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

            # --- NEUE LÖSUNG: Manuelle Breitenberechnung ---
            TS_WIDTH = 24
            PA_WIDTH = 10
            GA_WIDTH = 10
            PAYLOAD_WIDTH = 25
            COLUMN_SEPARATORS_WIDTH = 6 
            fixed_width = TS_WIDTH + PA_WIDTH + GA_WIDTH + PAYLOAD_WIDTH + COLUMN_SEPARATORS_WIDTH
            available_width = self.app.size.width
            # Puffer (4 Zeichen) für Scrollbar/Ränder
            remaining_width = available_width - fixed_width - 4
            name_width = max(10, remaining_width // 2)
            
            logging.debug(f"on_data_loaded: Terminalbreite={self.app.size.width}, Fix={fixed_width}, Rest={remaining_width}, NameWidth={name_width}")

            self.log_widget.add_column("Timestamp", key="ts", width=TS_WIDTH)
            self.log_widget.add_column("PA", key="pa", width=PA_WIDTH)
            self.log_widget.add_column("Gerät (PA)", key="pa_name", width=name_width) # Manuell
            self.log_widget.add_column("GA", key="ga", width=GA_WIDTH)
            self.log_widget.add_column("Gruppenadresse (GA)", key="ga_name", width=name_width) # Manuell
            self.log_widget.add_column("Payload", key="payload", width=PAYLOAD_WIDTH)
            # --- ENDE DER NEUEN LÖSUNG ---

            tabs.add_pane(TabPane("Gebäudestruktur", building_tree, id="building_pane"))
            tabs.add_pane(TabPane("Physikalische Adressen", pa_tree, id="pa_pane"))
            tabs.add_pane(TabPane("Gruppenadressen", ga_tree, id="ga_pane"))
            tabs.add_pane(TabPane("Log-Ansicht", self.log_widget, id="log_pane"))
            
            logging.debug("on_data_loaded: Populiere 'building_tree'...")
            self._populate_tree_from_data(building_tree, self.building_tree_data)
            logging.debug("on_data_loaded: Populiere 'pa_tree'...")
            self._populate_tree_from_data(pa_tree, self.pa_tree_data)
            logging.debug("on_data_loaded: Populiere 'ga_tree'...")
            # --- TIPPFELER KORRIGIERT ---
            self._populate_tree_from_data(ga_tree, self.ga_tree_data) 
            logging.debug("on_data_loaded: Bäume popoluiert. UI ist fast fertig.")

            tabs.disabled = False
            self.call_later(self._load_log_file_and_update_views)
        except Exception as e:
            logging.error(f"on_data_loaded: Kritischer Fehler beim UI-Aufbau: {e}", exc_info=True) 
            self.show_startup_error(e, traceback.format_exc())

    # --- DEBUG-LOGGING ---
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

    def _detect_log_format(self, first_lines: List[str]) -> Optional[str]:
        # (Unverändert)
        for line in first_lines:
            line = line.strip()
            if not line or line.startswith("="): continue
            if ' | ' in line and len(line.split('|')) > 4 and re.search(r'\d+/\d+/\d+', line.split('|')[3]):
                return 'pipe_separated'
            if ';' in line:
                return 'csv'
        return None

    def _parse_and_cache_log_data(self, lines: List[str]):
        """
        Parst die Log-Datei, aktualisiert das `self.payload_history` UND
        baut den `self.cached_log_data` Cache mit angereicherten Daten auf.
        """
        self.payload_history.clear()
        self.cached_log_data.clear()
        
        first_content_lines = [line for line in lines[:20] if line.strip() and not line.strip().startswith("=")]
        log_format = self._detect_log_format(first_content_lines)
        if not log_format:
            logging.warning("Konnte Log-Format beim Parsen für Cache nicht bestimmen.")
            return

        devices_dict = self.project_data.get("devices", {})
        ga_dict = self.project_data.get("group_addresses", {})

        for line in lines:
            clean_line = line.strip()
            if not clean_line: continue
            
            try:
                timestamp, pa, ga, payload = None, "N/A", None, None
                
                if log_format == 'pipe_separated':
                    parts = [p.strip() for p in clean_line.split('|')]
                    if len(parts) > 3:
                        timestamp = parts[0]
                        ga = parts[3]
                        pa = parts[1] if len(parts) > 1 else "N/A"
                        payload = parts[5] if len(parts) > 5 else None
                
                elif log_format == 'csv':
                    row = next(csv.reader([clean_line], delimiter=';'))
                    if len(row) > 4:
                        timestamp = row[0]
                        ga = row[4]
                        pa = row[1] if len(row) > 1 else "N/A"
                        payload = row[6] if len(row) > 6 else None
                
                if timestamp and ga and re.match(r'\d+/\d+/\d+', ga):
                    
                    if payload is not None:
                        if ga not in self.payload_history:
                            self.payload_history[ga] = []
                        self.payload_history[ga].append({'timestamp': timestamp, 'payload': payload})
                    
                    pa_name = devices_dict.get(pa, {}).get("name", "N/A")
                    ga_name = ga_dict.get(ga, {}).get("name", "N/A")
                    
                    self.cached_log_data.append({
                        "timestamp": timestamp,
                        "pa": pa,
                        "pa_name": pa_name,
                        "ga": ga,
                        "ga_name": ga_name,
                        "payload": payload if payload is not None else "N/A"
                    })

            except (IndexError, StopIteration, csv.Error) as e:
                logging.debug(f"Konnte Log-Zeile nicht parsen: '{clean_line}' - Fehler: {e}")
                continue
        
        for ga in self.payload_history:
            self.payload_history[ga].sort(key=lambda x: x['timestamp'])

    # --- PERFORMANCE-FIX (ADD_ROWS) + LIMITIERUNG (KEINE LADE-LOGIK) ---
    def _process_log_lines(self):
        """
        Filtert die in `self.cached_log_data` zwischengespeicherten,
        angereicherten Log-Einträge basierend auf `self.selected_gas`
        und füllt die `DataTable`.
        """
        if not self.log_widget: return
        
        try:
            self.log_widget.clear()

            has_selection = bool(self.selected_gas)
    
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
            else:
                logging.info("Keine Auswahl. Zeige alle Log-Einträge.")
                if len(self.cached_log_data) > MAX_LOG_LINES_NO_FILTER:
                    log_entries_to_process = self.cached_log_data[-MAX_LOG_LINES_NO_FILTER:]
                    log_caption = f"Alle Einträge (Letzte {MAX_LOG_LINES_NO_FILTER} von {len(self.cached_log_data)} angezeigt)"
                    logging.warning(f"Kein Filter aktiv. Zeige nur die letzten {MAX_LOG_LINES_NO_FILTER} von {len(self.cached_log_data)} Log-Einträgen.")
                else:
                    log_caption = f"Alle Einträge ({len(self.cached_log_data)})"

            found_count = 0
            rows_to_add = []
            for i, log_entry in enumerate(log_entries_to_process):
                if has_selection and log_entry["ga"] not in self.selected_gas:
                    continue 
                rows_to_add.append((
                    log_entry["timestamp"],
                    log_entry["pa"],
                    log_entry["pa_name"],
                    log_entry["ga"],
                    log_entry["ga_name"],
                    log_entry["payload"]
                ))
                found_count += 1
            
            self.log_widget.add_rows(rows_to_add)
            
            duration = time.time() - start_time
            logging.info(f"Log-Ansicht gefiltert. {found_count} Einträge in {duration:.4f}s gefunden.")
            self.log_widget.caption = f"{found_count} Einträge gefunden. ({duration:.2f}s) | {log_caption}"
        
        except Exception as e:
            # Fallback-Fehlerbehandlung
            logging.error(f"Schwerer Fehler in _process_log_lines: {e}", exc_info=True)
            if self.log_widget:
                self.log_widget.clear()
                self.log_widget.add_row(f"[red]Fehler beim Verarbeiten der Log-Zeilen: {e}[/red]")


    # --- ANGEPASSTE FUNKTION (KEINE LADE-LOGIK) ---
    def _load_log_file_and_update_views(self):
        """Liest die Log-Datei von der Festplatte, aktualisiert den Cache und alle Ansichten."""
        if not self.log_widget: return
        log_widget = self.log_widget
        
        log_file_path = self.config.get("log_file") or os.path.join(self.config.get("log_path", "."), "knx_bus.log")

        if not os.path.exists(log_file_path):
            log_widget.clear()
            log_widget.add_row(f"[red]FEHLER: Log-Datei nicht gefunden unter '{log_file_path}'[/red]")
            log_widget.add_row("[dim]Mit 'o' eine andere Datei öffnen.[/dim]")
            self.cached_log_data = []
            self.payload_history.clear()
            return
        
        start_time = time.time()
        logging.info(f"Lese Log-Datei von Festplatte: '{log_file_path}'")
        
        try:
            # Datei einlesen
            lines = []
            if log_file_path.lower().endswith(".zip"):
                with zipfile.ZipFile(log_file_path, 'r') as zf:
                    log_files_in_zip = [name for name in zf.namelist() if name.lower().endswith('.log')]
                    if not log_files_in_zip:
                        log_widget.clear()
                        log_widget.add_row(f"\n[red]Keine .log-Datei im ZIP-Archiv gefunden.[/red]")
                        self.cached_log_data = []
                        self.payload_history.clear()
                        return
                    with zf.open(log_files_in_zip[0]) as log_file:
                        lines = io.TextIOWrapper(log_file, encoding='utf-8').readlines()
            else:
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            
            # --- DATATABLE ANPASSUNG (Synchron) ---
            # 1. Parsen und Cachen (langsam)
            logging.debug("Starte _parse_and_cache_log_data...")
            self._parse_and_cache_log_data(lines)
            logging.debug("Beende _parse_and_cache_log_data.")
            
            # 2. Baum-Labels aktualisieren
            logging.debug("Aktualisiere Baum-Labels...")
            for tree in self.query(Tree):
                self._update_tree_labels_recursively(tree.root)
            logging.debug("Baum-Labels aktualisiert.")
            
            # 3. Log-Ansicht filtern und rendern (langsam)
            logging.debug("Starte _process_log_lines...")
            self._process_log_lines()
            logging.debug("Beende _process_log_lines.")
            # --- ENDE ANPASSUNG ---

            duration = time.time() - start_time
            logging.info(f"Log-Datei '{os.path.basename(log_file_path)}' in {duration:.2f}s gelesen und verarbeitet.")

        except Exception as e:
            log_widget.clear()
            log_widget.add_row(f"\n[red]Fehler beim Verarbeiten der Log-Datei: {e}[/red]")
            log_widget.add_row(f"[dim]{traceback.format_exc()}[/dim]")
            logging.error(f"Fehler beim Verarbeiten von '{log_file_path}': {e}", exc_info=True)
            self.cached_log_data = []
            self.payload_history.clear()
            
    # --- ANGEPASSTE FUNKTION (KEINE LADE-LOGIK) ---
    def _refilter_log_view(self) -> None:
        """Filtert die bereits geladenen Log-Zeilen neu, ohne die Datei erneut zu lesen."""
        if not self.log_widget: return
        
        logging.info("Log-Ansicht wird mit gecachten Daten neu gefiltert (synchron).")
        
        # Starte "2. MACHEN" direkt
        logging.debug("Starte _process_log_lines...")
        self._process_log_lines()
        logging.debug("Beende _process_log_lines.")

    def _get_descendant_gas(self, node: TreeNode) -> Set[str]:
        # (Unverändert)
        gas = set()
        if node.data and "gas" in node.data:
            gas.update(node.data["gas"])
        for child in node.children:
            gas.update(self._get_descendant_gas(child))
        return gas

    def _update_tree_labels_recursively(self, node: TreeNode) -> None:
        # (Unverändert)
        display_label = ""
        if node.data and "original_name" in node.data:
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
            display_label = re.sub(r"^(\[[ *\-]] )+", "", node.label.plain)

        prefix = ""
        all_descendant_gas = self._get_descendant_gas(node)
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            if not selected_descendant_gas: 
                prefix = "[ ] "
            elif len(selected_descendant_gas) == len(all_descendant_gas): 
                prefix = "[*] "
            else: 
                prefix = "[-] "

        node.set_label(prefix + display_label)

        for child in node.children:
            self._update_tree_labels_recursively(child)

    def action_toggle_selection(self) -> None:
        # (Unverändert)
        try:
            active_tree = self.query_one(TabbedContent).active_pane.query_one(Tree)
            node = active_tree.cursor_node
            if not node: return
            descendant_gas = self._get_descendant_gas(node)
            if not descendant_gas: return
            
            node_label = re.sub(r"^(\[[ *\-]] )+", "", node.label.plain)
            if descendant_gas.issubset(self.selected_gas):
                logging.info(f"Auswahl ENTFERNT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) entfernt.")
                self.selected_gas.difference_update(descendant_gas)
            else:
                logging.info(f"Auswahl HINZUGEFÜGT für Knoten '{node_label}'. {len(descendant_gas)} GA(s) hinzugefügt.")
                self.selected_gas.update(descendant_gas)
            
            for tree in self.query(Tree):
                self._update_tree_labels_recursively(tree.root)
            
            if self.query_one(TabbedContent).active == "log_pane":
                self._refilter_log_view()

        except Exception as e:
            logging.error(f"Fehler bei action_toggle_selection: {e}", exc_info=True)
            
    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        # (Unverändert)
        if event.pane.id == "log_pane":
            logging.info("Log-Ansicht-Tab wurde aktiviert. Wende aktuellen Filter neu an.")
            self._refilter_log_view()

    def action_copy_label(self) -> None:
        # (Unverändert)
        try:
            active_tree = self.query_one(TabbedContent).active_pane.query_one(Tree)
            node = active_tree.cursor_node
            if node and node.data and "original_name" in node.data:
                self.notify(f"Kopiert: '{node.data['original_name']}'")
            elif node:
                label_text = node.label.plain
                clean_label = re.sub(r"^(\[[ *\-]] )+", "", label_text)
                clean_label = re.sub(r"\s*->\s*.*$", "", clean_label)
                self.notify(f"Kopiert: '{clean_label}'")
        except Exception:
            self.notify("Konnte nichts kopieren.", severity="error")

    def action_open_log_file(self) -> None:
        # (Unverändert)
        def handle_open_result(result: Tuple[str, bool]):
            path, should_save = result
            if not path:
                self.notify("Öffnen abgebrochen.", severity="warning")
                return
            
            if not os.path.exists(path):
                self.notify(f"Datei nicht gefunden: {path}", severity="error", timeout=5)
                return

            self.config['log_file'] = path
            self.notify(f"Log-Datei geöffnet: {os.path.basename(path)}")

            if should_save:
                dotenv_path = find_dotenv() or Path(".env")
                if not os.path.exists(dotenv_path): Path(dotenv_path).touch()
                set_key(str(dotenv_path), "LOG_FILE", path, quote_mode="never")
                self.notify("Pfad als neuen Standard gespeichert.", severity="information")
                logging.info(f"Neuer Standard-Log-Pfad gespeichert: {path}")

            self._load_log_file_and_update_views()
        self.push_screen(OpenFileScreen(), handle_open_result)

    def action_reload_log_file(self) -> None:
        # (UnverGändert)
        logging.info("Log-Datei wird manuell von Festplatte neu geladen.")
        self._load_log_file_and_update_views()
    
    def action_toggle_log_reload(self) -> None:
        # (Unverändert)
        if self.log_reload_timer:
            self.log_reload_timer.stop()
            self.log_reload_timer = None
            self.notify("Log Auto-Reload [bold red]AUS[/].", title="Log Ansicht")
            logging.info("Auto-Reload für Log-Datei deaktiviert.")
        else:
            self.log_reload_timer = self.set_interval(1, self._load_log_file_and_update_views)
            self.notify("Log Auto-Reload [bold green]EIN[/].", title="Log Ansicht")
            logging.info("Auto-Reload für Log-Datei im 1-Sekunden-Intervall aktiviert.")

    def _filter_tree_data(self, original_data: TreeData, filter_text: str) -> Tuple[Optional[TreeData], bool]:
        # (Unverändert)
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
        # (Unverändert)
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
        # (Unverändert)
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

    # --- NEUE FUNKTION (LAYOUT-FIX V8) ---
    def on_resize(self, event: events.Resize) -> None:
        """Fenstergröße hat sich geändert. Berechne Spalten neu."""
        
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
    # --- ENDE NEUE FUNKTION ---

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

        # --- CSS-VERSION V8 (Breiten in Python, CSS nur für Overflow) ---
        css_content = """
        #loading_label { width: 100%; height: 100%; content-align: center middle; padding: 1; }
        #filter_dialog, #open_file_dialog { width: 80%; max-width: 70; height: auto; padding: 1 2; background: $surface; border: heavy $primary; }
        #filter_dialog > Label, #open_file_dialog > Label { margin-bottom: 1; }
        #filter_input, #path_input { background: $boost; }
        
        TabbedContent {
            height: 1fr;
        }

        #log_view {
            width: 100%;
        }

        /* Style für die Fußzeile (Caption) der Tabelle */
        DataTable > .datatable--caption {
            width: 100%;
            text-align: center;
            color: $text-muted;
        }

        /* --- LÖSUNG (V8): Breiten werden in Python berechnet --- */
        
        /* WICHTIG: Wir brauchen 'overflow' hier, damit der Text,
           der länger als die berechnete Spaltenbreite ist,
           abgeschnitten wird ('...'). */
        #log_widget .datatable--column-key-pa_name,
        #log_widget .datatable--column-key-ga_name {
            overflow: hidden;
            text-overflow: ellipsis;
        }
        /* Alle 'width:'-Definitionen für Spalten sind entfernt. */
        /* --- ENDE DER LÖSUNG --- */

        #open_file_dialog > Horizontal { height: auto; align: center middle; margin-top: 1; }
        """
        # --- ENDE CSS-VERSION V8 ---
        
        with open("knx-lens.css", "w", encoding='utf-8') as f: f.write(css_content)

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