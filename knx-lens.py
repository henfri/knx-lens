#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Ein interaktiver KNX Projekt-Explorer und Log-Filter.
Ermöglicht das Browsen des Projekts nach Gebäude-, Physikalischer und Gruppen-Struktur.
"""
import json
import csv
import argparse
import os
import hashlib
import sys
import traceback
import re
from typing import Dict, List, Any, Optional, Set, Tuple

# Third-party libraries
from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Center
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, Tree, Static, Input, TabbedContent, TabPane, RichLog, Label
from textual.widgets.tree import TreeNode
from xknxproject import XKNXProj

### --- SETUP & KONSTANTEN ---
TreeData = Dict[str, Any]

### --- KERNLOGIK: PARSING & DATENSTRUKTURIERUNG ---

def get_md5_hash(file_path: str) -> str:
    """Berechnet den MD5-Hash einer Datei."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def load_or_parse_project(knxproj_path: str, password: Optional[str]) -> Dict:
    """Lädt ein KNX-Projekt aus dem Cache oder parst es neu."""
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
                    print("Cache ist aktuell. Lade aus dem Cache...")
                    project_data = cache_data["project"]
            except (json.JSONDecodeError, KeyError):
                print("Cache ist korrupt. Parse Projekt neu...")
    
    if not project_data:
        print(f"Parse KNX-Projektdatei: {knxproj_path} (dies kann einen Moment dauern)...")
        # Korrigierter Aufruf von XKNXProj
        xknxproj = XKNXProj(knxproj_path, password=password) 
        try:
            project_data = xknxproj.parse()
        except Exception as e: # Besser eine spezifischere Exception fangen, falls bekannt
             print(f"Fehler beim Parsen: {e}")
             # Hier könnte eine Passwortabfrage oder ein erneuter Versuch implementiert werden
             raise e

        current_md5 = get_md5_hash(knxproj_path)
        new_cache_data = {"md5": current_md5, "project": project_data}
        print(f"Speichere neuen Cache nach {cache_path}")
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(new_cache_data, f)
    
    if 'project' in project_data:
        project_data = project_data['project']

    print(f"DEBUG: Geladene Projekt-Schlüssel: {list(project_data.keys())}")
    return project_data

def get_best_channel_name(channel: Dict, ch_id: str) -> str:
    """Wählt den besten verfügbaren Namen für einen Kanal aus."""
    return channel.get("function_text") or channel.get("name") or f"Kanal-{ch_id}"

def add_com_objects_to_node(parent_node: Dict, com_obj_ids: List[str], project_data: Dict):
    """Fügt einem Eltern-Knoten (Gerät oder Kanal) Kommunikationsobjekte hinzu."""
    comm_objects = project_data.get("communication_objects", {})
    for co_id in com_obj_ids:
        co = comm_objects.get(co_id)
        if co:
            co_name = co.get("name", f"CO-{co_id}")
            gas = co.get("group_address_links", [])
            gas_str = ", ".join(gas)
            co_label = f"{co_name} → {gas_str}"
            parent_node["children"][co_label] = {
                "id": f"co_{co_id}", "name": co_label,
                "data": {"type": "co", "gas": set(gas)}, "children": {}
            }

def build_ga_tree_data(project: Dict) -> TreeData:
    """
    Baut den GA-Baum hierarchisch aus group_ranges und group_addresses auf,
    um alle Ebenen korrekt zu benennen.
    """
    print("DEBUG: Starte build_ga_tree_data...")
    root_node: TreeData = {"id": "ga_root", "name": "Gruppenadressen", "children": {}}
    
    group_ranges = project.get("group_ranges", {})
    group_addresses = project.get("group_addresses", {})
    hierarchy = {}
    
    for key, value in group_ranges.items():
        parts = key.split('/')
        name = value.get("name", f"Bereich {key}")
        if len(parts) == 1:
            if key not in hierarchy:
                hierarchy[key] = {"name": name, "subgroups": {}}
            else:
                hierarchy[key]["name"] = name
        elif len(parts) == 2:
            main_key = parts[0]
            if main_key not in hierarchy:
                hierarchy[main_key] = {"name": f"Hauptgruppe {main_key}", "subgroups": {}}
            if key not in hierarchy[main_key]["subgroups"]:
                hierarchy[main_key]["subgroups"][key] = {"name": name, "addresses": {}}
            else:
                hierarchy[main_key]["subgroups"][key]["name"] = name

    for address, details in group_addresses.items():
        parts = address.split('/')
        if len(parts) != 3: continue
        main_key, sub_key_part, _ = parts
        sub_key = f"{main_key}/{sub_key_part}"
        name = details.get("name", "N/A")
        if main_key not in hierarchy:
            hierarchy[main_key] = {"name": f"Hauptgruppe {main_key}", "subgroups": {}}
        if sub_key not in hierarchy[main_key]["subgroups"]:
            hierarchy[main_key]["subgroups"][sub_key] = {"name": f"Mittelgruppe {sub_key}", "addresses": {}}
        hierarchy[main_key]["subgroups"][sub_key]["addresses"][address] = {"name": name}

    sorted_main_keys = sorted(hierarchy.keys(), key=int)
    for main_key in sorted_main_keys:
        main_group = hierarchy[main_key]
        main_node = root_node["children"].setdefault(main_key, {
            "id": f"ga_main_{main_key}",
            "name": f"({main_key}) {main_group['name']}",
            "children": {}
        })
        sorted_sub_keys = sorted(main_group["subgroups"].keys(), key=lambda k: [int(p) for p in k.split('/')])
        for sub_key in sorted_sub_keys:
            sub_group = main_group["subgroups"][sub_key]
            sub_node = main_node["children"].setdefault(sub_key, {
                "id": f"ga_sub_{sub_key.replace('/', '_')}",
                "name": f"({sub_key}) {sub_group['name']}",
                "children": {}
            })
            sorted_addresses = sorted(sub_group.get("addresses", {}).items(), key=lambda item: [int(p) for p in item[0].split('/')])
            for addr_str, addr_details in sorted_addresses:
                leaf_name = f"({addr_str}) {addr_details['name']}"
                sub_node["children"][addr_str] = {
                    "id": f"ga_{addr_str}",
                    "name": leaf_name,
                    "data": {"type": "ga", "gas": {addr_str}},
                    "children": {}
                }
    print("DEBUG: Beende build_ga_tree_data.")
    return root_node

def build_pa_tree_data(project: Dict) -> TreeData:
    """Baut den PA-Baum auf, inkl. KOs auf Geräte- und Kanalebene."""
    print("DEBUG: Starte build_pa_tree_data...")
    pa_tree = {"children": {}}
    devices = project.get("devices", {})
    for pa, device in devices.items():
        parts = pa.split('.')
        node = pa_tree
        
        # Traverse/create Area and Line nodes
        for i in range(2):  # 0 for Area, 1 for Line
            part = parts[i]
            current_path = ".".join(parts[:i+1])
            node_name = f"Bereich {current_path}" if i == 0 else f"Linie {current_path}"
            if part not in node["children"]:
                node["children"][part] = {"id": f"pa_{current_path}", "name": node_name, "children": {}}
            node = node["children"][part]
        
        # Now `node` is the Line node. Add the device to it.
        device_part = parts[2]
        device_name = f"({pa}) {device['name']}"
        device_node = node["children"].setdefault(device_part, {"id": f"pa_{pa}", "name": device_name, "children": {}})
        
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
    print("DEBUG: Beende build_pa_tree_data.")
    return pa_tree

def build_building_tree_data(project: Dict) -> TreeData:
    """Baut den Baum aus der 'locations'-Struktur des Projekts auf."""
    print("DEBUG: Starte build_building_tree_data...")
    building_tree = {"children": {}}
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
    print("DEBUG: Beende build_building_tree_data.")
    return building_tree

### --- TUI: SCREENS & MODALS ---
class FilterInputScreen(ModalScreen[str]):
    """Ein modaler Bildschirm für die Filtereingabe."""
    
    def compose(self) -> ComposeResult:
        yield Center(
            Vertical(
                Label("Baum filtern (Enter zum Bestätigen, ESC zum Abbrechen):"),
                Input(placeholder="Filtertext...", id="filter_input"),
                id="filter_dialog"
            )
        )

    def on_mount(self) -> None:
        """Fokussiert das Eingabefeld beim Öffnen."""
        self.query_one("#filter_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Schließt den Dialog und gibt den Filtertext zurück."""
        self.dismiss(event.value)

    def on_key(self, event: "events.Key") -> None:
        """Bei ESC-Taste abbrechen."""
        if event.key == "escape":
            self.dismiss("")

### --- TUI: HAUPTANWENDUNG ---
class KNXExplorerApp(App):
    CSS_PATH = "knx_tui_tool.css"
    BINDINGS = [
        Binding("q", "quit", "Beenden"),
        Binding("a", "toggle_selection", "Auswahl umschalten"),
        Binding("f", "filter_tree", "Aktuellen Baum filtern"),
        Binding("escape", "reset_filter", "Filter zurücksetzen", show=True),
    ]

    def __init__(self, config: Dict):
        super().__init__()
        self.config = config
        self.project_data: Optional[Dict] = None
        self.building_tree_data: TreeData = {}
        self.pa_tree_data: TreeData = {}
        self.ga_tree_data: TreeData = {}
        self.selected_gas: Set[str] = set()
        self.log_widget: Optional[RichLog] = None

    def compose(self) -> ComposeResult:
        yield Header(name="KNX Projekt-Explorer")
        yield Vertical(Static("Lade und verarbeite Projektdatei...", id="loading_label"))
        yield TabbedContent(id="main_tabs", disabled=True)
        yield Footer()

    def show_startup_error(self, exc: Exception, tb_str: str) -> None:
        try:
            loading_label = self.query_one("#loading_label")
            loading_label.update(f"[bold red]FATALER FEHLER BEIM LADEN[/]\n\n"
                                 f"[yellow]Fehlertyp:[/] {type(exc).__name__}\n"
                                 f"[yellow]Meldung:[/] {exc}\n\n"
                                 f"[bold]Traceback:[/]\n{tb_str}")
        except Exception:
            self.exit(f"Fehler im Fehler-Handler!\nOriginaler Fehler: {exc}")

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
            tb_str = traceback.format_exc()
            self.call_from_thread(self.show_startup_error, e, tb_str)
    
    def on_data_loaded(self) -> None:
        try:
            self.query_one("#loading_label").remove()
            tabs = self.query_one(TabbedContent)
            
            building_tree = Tree("Gebäude", id="building_tree")
            pa_tree = Tree("Linien", id="pa_tree")
            ga_tree = Tree("Funktionen", id="ga_tree")
            self.log_widget = RichLog(highlight=True, markup=True, id="log_view")

            tabs.add_pane(TabPane("Gebäudestruktur", building_tree))
            tabs.add_pane(TabPane("Physikalische Adressen", pa_tree))
            tabs.add_pane(TabPane("Gruppenadressen", ga_tree))
            tabs.add_pane(TabPane("Log", self.log_widget))
            
            self._populate_tree_from_data(building_tree, self.building_tree_data)
            self._update_tree_visuals(building_tree.root)
            self._populate_tree_from_data(pa_tree, self.pa_tree_data)
            self._update_tree_visuals(pa_tree.root)
            self._populate_tree_from_data(ga_tree, self.ga_tree_data)
            self._update_tree_visuals(ga_tree.root)

            tabs.disabled = False
            self.call_later(self._update_log_view)
        except Exception as e:
            tb_str = traceback.format_exc()
            self.show_startup_error(e, tb_str)

    def _populate_tree_from_data(self, tree: Tree, data: TreeData):
        """Füllt einen Baum rekursiv aus einer verschachtelten Dictionary-Struktur."""
        def natural_sort_key(item: Tuple[str, Any]):
            key_str = str(item[0])
            parts = [int(c) if c.isdigit() else c.lower() for c in re.split('([0-9]+)', key_str)]
            return parts

        def add_nodes(parent_node: TreeNode, children_data: Dict[str, TreeData]):
            sorter = sorted(children_data.items(), key=natural_sort_key)
            for _, node_data in sorter:
                label = node_data.get("name")
                if not label: continue
                node_children = node_data.get("children", {})
                if node_children:
                    child_node = parent_node.add(label, data=node_data.get("data"))
                    add_nodes(child_node, node_children)
                else:
                    parent_node.add_leaf(label, data=node_data.get("data"))
        
        add_nodes(tree.root, data["children"])
        tree.root.expand()

    # --- NEU: Funktion zur automatischen Erkennung des Log-Formats ---
    def _detect_log_format(self, file_path: str) -> Optional[str]:
        """
        Analysiert die ersten Zeilen einer Log-Datei, um das Format zu erkennen.
        Gibt 'csv', 'pipe_separated' oder None zurück.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for _ in range(10):  # Überprüfe die ersten 10 Zeilen
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Kriterium für das neue Format: Enthält ' | ' und die GA-Struktur
                    parts = line.split('|')
                    if ' | ' in line and len(parts) > 3 and re.match(r'\s*\d+/\d+/\d+\s*', parts[3]):
                        return 'pipe_separated'
                    
                    # Kriterium für das alte Format: Enthält Semikolon
                    if ';' in line:
                        return 'csv'
        except (FileNotFoundError, UnicodeDecodeError):
            return None
        return None # Wenn nach 10 Zeilen nichts erkannt wurde

    # --- MODIFIZIERT: Log-Ansicht mit automatischer Formaterkennung ---
    def _update_log_view(self):
        if not self.log_widget: return
        log_widget = self.log_widget
        log_widget.clear()
        
        log_file_path = self.config.get("log_file")
        if not log_file_path:
            log_widget.write("[red]Kein Log-Dateipfad konfiguriert.[/red]")
            return
            
        if not os.path.exists(log_file_path):
            log_widget.write(f"\n[red]FEHLER: Log-Datei nicht gefunden unter '{log_file_path}'[/red]")
            return
            
        if self.selected_gas:
            sorted_gas = sorted(list(self.selected_gas))
            log_widget.write(f"[dim]Filtere Log für {len(sorted_gas)} GAs: {', '.join(sorted_gas)}[/dim]\n")
        else:
            log_widget.write("[yellow]Keine Gruppenadressen für den Filter ausgewählt. Mit 'a' umschalten.[/yellow]")
            return

        try:
            # Formaterkennung aufrufen
            log_format = self._detect_log_format(log_file_path)

            if log_format is None:
                log_widget.write(f"[red]Konnte das Log-Format von '{os.path.basename(log_file_path)}' nicht bestimmen.[/red]")
                log_widget.write("[dim]Unterstützte Formate: CSV (mit ';') oder Pipe-getrennt ('Timestamp | ... | GA | ...').[/dim]")
                return

            found_count = 0
            with open(log_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    clean_line = line.strip()
                    if not clean_line:
                        continue

                    ga_to_check = None
                    
                    # Logik basierend auf dem erkannten Format anwenden
                    if log_format == 'csv':
                        try:
                            # csv.reader erwartet eine Liste, daher [clean_line]
                            row = next(csv.reader([clean_line], delimiter=';'))
                            if len(row) > 4:
                                ga_to_check = row[4].strip()
                        except (csv.Error, StopIteration):
                            continue # Fehlerhafte Zeile ignorieren
                    
                    elif log_format == 'pipe_separated':
                        parts = clean_line.split('|')
                        if len(parts) > 3:
                            # Die GA ist das 4. Element (Index 3)
                            ga_to_check = parts[3].strip()

                    # Filterung durchführen und Originalzeile ausgeben
                    if ga_to_check and ga_to_check in self.selected_gas:
                        log_widget.write(line.rstrip())
                        found_count += 1
                        
            log_widget.write(f"\n[green]{found_count} passende Einträge gefunden.[/green]")
        except Exception as e:
            log_widget.write(f"\n[red]Ein Fehler beim Verarbeiten der Log-Datei ist aufgetreten: {e}[/red]")
            log_widget.write(f"[dim]{traceback.format_exc()}[/dim]")

    def _get_descendant_gas(self, node: TreeNode) -> Set[str]:
        """Sammelt rekursiv alle GAs von einem Knoten und seinen Kindern."""
        gas = set()
        if node.data and "gas" in node.data:
            gas.update(node.data["gas"])
        for child in node.children:
            gas.update(self._get_descendant_gas(child))
        return gas

    def _update_tree_visuals(self, node: TreeNode) -> None:
        """
        Aktualisiert rekursiv die Checkboxen für einen Knoten und seine Kinder.
        [ ] = Keine GA ausgewählt, [-] = Einige GAs ausgewählt, [*] = Alle GAs ausgewählt.
        """
        all_descendant_gas = self._get_descendant_gas(node)
        
        label_text = node.label.plain
        # Altes Präfix entfernen
        if label_text.startswith(("[ ] ", "[*] ", "[-] ")):
            label = label_text[4:]
        else:
            label = label_text

        # Nur Knoten, die GAs repräsentieren, erhalten eine Checkbox.
        if all_descendant_gas:
            selected_descendant_gas = self.selected_gas.intersection(all_descendant_gas)
            
            prefix = ""
            if not selected_descendant_gas:
                prefix = "[ ] "
            elif len(selected_descendant_gas) == len(all_descendant_gas):
                prefix = "[*] "
            else:
                prefix = "[-] "
            node.set_label(prefix + label)
        else:
            # Stellt sicher, dass Knoten ohne GAs keinen Präfix haben
            node.set_label(label)

        for child in node.children:
            self._update_tree_visuals(child)

    def action_toggle_selection(self) -> None:
        """Schaltet die Auswahl für den aktuellen Knoten und alle Kinder um."""
        try:
            active_pane = self.query_one(TabbedContent).active_pane
            active_tree = active_pane.query_one(Tree)
            node = active_tree.cursor_node
            if not node: return

            descendant_gas = self._get_descendant_gas(node)
            if not descendant_gas: return

            is_fully_selected = descendant_gas.issubset(self.selected_gas)

            if is_fully_selected:
                self.selected_gas.difference_update(descendant_gas)
            else:
                self.selected_gas.update(descendant_gas)
            
            self._update_tree_visuals(active_tree.root)
            self.call_later(self._update_log_view)
        except Exception:
            pass

    def action_filter_tree(self) -> None:
        """Öffnet den Filterdialog und wendet den Filter an."""
        def apply_filter(filter_text: str):
            if filter_text is None:
                return  # Dialog wurde abgebrochen
            
            try:
                active_pane = self.query_one(TabbedContent).active_pane
                if not active_pane: return
                active_tree = active_pane.query_one(Tree)
                
                if not filter_text:
                    self.action_reset_filter()
                else:
                    text_to_find = filter_text.lower()
                    visible_nodes = set()
                    self._collect_visible_nodes_pass(active_tree.root, text_to_find, visible_nodes)
                    self._apply_visibility_pass(active_tree.root, visible_nodes)

            except Exception as e:
                if self.log_widget:
                    self.log_widget.write(f"[red]Filter-Fehler: {e}[/red]")

        self.push_screen(FilterInputScreen(), apply_filter)

    def _collect_visible_nodes_pass(self, node: TreeNode, filter_text: str, visible_set: Set[TreeNode]) -> bool:
        """Sammelt alle Knoten, die sichtbar sein sollen, in 'visible_set'."""
        label_text = node.label.plain
        search_text = label_text[4:] if label_text.startswith(("[ ] ", "[*] ", "[-] ")) else label_text
        search_text = search_text.lower()
        child_matches = [self._collect_visible_nodes_pass(child, filter_text, visible_set) for child in node.children]
        has_matching_child = any(child_matches)
        node_matches = filter_text in search_text
        if node_matches or has_matching_child:
            visible_set.add(node)
            if node_matches:
                for child in node.children:
                    self._add_all_descendants(child, visible_set)
            return True
        return False

    def _add_all_descendants(self, node: TreeNode, visible_set: Set[TreeNode]):
        """Fügt einen Knoten und alle Nachkommen rekursiv zu einem Set hinzu."""
        visible_set.add(node)
        for child in node.children:
            self._add_all_descendants(child, visible_set)
    
    def _apply_visibility_pass(self, node: TreeNode, visible_set: Set[TreeNode]):
        """Setzt die 'visible' Eigenschaft für jeden Knoten und klappt passende auf."""
        is_visible = node in visible_set
        node.visible = is_visible
        if is_visible:
            has_visible_children = any(child in visible_set for child in node.children)
            if has_visible_children:
                node.expand()
            else:
                node.collapse()
            for child in node.children:
                self._apply_visibility_pass(child, visible_set)

    def action_reset_filter(self) -> None:
        """Setzt den Filter zurück und zeigt alle Knoten an."""
        try:
            active_pane = self.query_one(TabbedContent).active_pane
            if not active_pane: return
            active_tree = active_pane.query_one(Tree)
            def reset_node(node: TreeNode):
                node.visible = True
                node.expand()
                for child in node.children:
                    reset_node(child)
            reset_node(active_tree.root)
        except Exception:
            pass


### --- START ---
def main():
    css_content = """
#loading_label {
    width: 100%;
    height: 100%;
    content-align: center middle;
    padding: 1;
}
#filter_dialog {
    width: 80%;
    max-width: 70;
    height: auto;
    padding: 1 2;
    background: $surface;
    border: heavy $primary;
}
#filter_dialog > Label {
    margin-bottom: 1;
}
#filter_input {
    background: $boost;
}
#log_view {
    border: round white;
    padding: 1;
}
"""
    with open("knx_tui_tool.css", "w") as f:
        f.write(css_content)
            
    load_dotenv()
    parser = argparse.ArgumentParser(description="KNX Projekt-Explorer und Log-Filter.")
    parser.add_argument("--path", help="Pfad zur .knxproj Datei (überschreibt KNX_PROJECT_PATH aus .env)")
    parser.add_argument("--log-file", help="Pfad zur Log-Datei für die Filterung (überschreibt LOG_FILE aus .env)")
    parser.add_argument("--password", help="Passwort für die Projektdatei (überschreibt KNX_PASSWORD aus .env)")
    args = parser.parse_args()
    config = {
        'knxproj_path': args.path or os.getenv('KNX_PROJECT_PATH'),
        'log_file': args.log_file or os.getenv('LOG_FILE'),
        'password': args.password or os.getenv('KNX_PASSWORD')
    }
    if not config['knxproj_path']:
        print("FEHLER: Projektpfad ist erforderlich. Bitte mit --path angeben oder in .env-Datei als KNX_PROJECT_PATH setzen.", file=sys.stderr)
        sys.exit(1)
    
    app = KNXExplorerApp(config=config)
    app.run()

if __name__ == "__main__":
    main()