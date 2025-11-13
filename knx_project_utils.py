#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hilfsfunktionen zum Parsen und Strukturieren von KNX-Projektdaten.
Wird von knx-lens.py importiert.
"""

import json
import os
import hashlib
import time
import logging
import re
from typing import Dict, List, Any, Optional, Set, Tuple
from xknxproject import XKNXProj

TreeData = Dict[str, Any]

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

def _get_smart_name(data_dict: Dict, fallback: str) -> str:
    """
    Interne Hilfsfunktion: Baut einen Namen aus 'text' und 'function_text'.
    Fallback auf 'name' oder den übergebenen Fallback-String.
    """
    parts = []
    
    # 1. Priorität: Kombination aus "text" und "function_text"
    if val := data_dict.get("text"):
        parts.append(str(val).strip())
    
    if val := data_dict.get("function_text"):
        parts.append(str(val).strip())
        
    if parts:
        return " - ".join(parts)
    
    # 2. Priorität: "name" (oft kryptisch wie "LOG_KOf10O")
    if val := data_dict.get("name"):
        return str(val).strip()
        
    # 3. Priorität: Fallback
    return fallback

def get_best_channel_name(channel: Dict, ch_id: str) -> str:
    """Ermittelt den besten Namen für einen Kanal."""
    return _get_smart_name(channel, f"Kanal-{ch_id}")

def add_com_objects_to_node(parent_node: Dict, com_obj_ids: List[str], project_data: Dict):
    """Fügt Communication Objects als Kinder zu einem Knoten hinzu."""
    comm_objects = project_data.get("communication_objects", {})
    for co_id in com_obj_ids:
        co = comm_objects.get(co_id)
        if co:
            # --- KORREKTUR: Hier nutzen wir jetzt auch die intelligente Namensfindung ---
            co_name = _get_smart_name(co, f"CO-{co_id}")
            # --- ENDE KORREKTUR ---
            
            gas = co.get("group_address_links", [])
            gas_str = ", ".join([str(g) for g in gas])
            
            co_number = co.get('number', '?')
            co_label = f"{co_number}: {co_name} → [{gas_str}]"
            
            parent_node["children"][co_label] = {
                "id": f"co_{co_id}", 
                "name": co_label,
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
        mg_name = main_group.get('name') or f'HG {main_key}'
        main_node_name = f"({main_key}) {mg_name}"
        main_node = root_node["children"].setdefault(main_key, {"id": f"ga_main_{main_key}", "name": main_node_name, "children": {}})

        sorted_sub_keys = sorted(main_group.get("subgroups", {}).keys(), key=lambda k: [int(p) for p in k.split('/')])
        for sub_key in sorted_sub_keys:
            sub_group = main_group["subgroups"][sub_key]
            sg_name = sub_group.get('name') or f'MG {sub_key}'
            sub_node_name = f"({sub_key}) {sg_name}"
            sub_node = main_node["children"].setdefault(sub_key, {"id": f"ga_sub_{sub_key.replace('/', '_')}", "name": sub_node_name, "children": {}})

            sorted_addresses = sorted(sub_group.get("addresses", {}).items(), key=lambda item: [int(p) for p in item[0].split('/')])
            for addr_str, addr_details in sorted_addresses:
                addr_name = addr_details.get('name') or 'N/A'
                leaf_name = f"({addr_str}) {addr_name}"
                sub_node["children"][addr_str] = {
                    "id": f"ga_{addr_str}", 
                    "name": leaf_name, 
                    "data": {"type": "ga", "gas": {addr_str}, "original_name": leaf_name}, 
                    "children": {}
                }
    
    return root_node

def build_pa_tree_data(project: Dict) -> TreeData:
    pa_tree = {"id": "pa_root", "name": "Physikalische Adressen", "children": {}}
    devices = project.get("devices", {})
    topology = project.get("topology", {})
    
    area_names = {str(area['address']): (area.get('name') or '') for area in topology.get("areas", {}).values()}
    line_names = {}
    for area in topology.get("areas", {}).values():
        for line in area.get("lines", {}).values():
            line_id = f"{area['address']}.{line['address']}"
            line_names[line_id] = (line.get('name') or '')

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

        dev_name = device.get('name') or 'N/A'
        device_name = f"({pa}) {dev_name}"
        device_node = line_node["children"].setdefault(dev_id, {"id": f"dev_{pa}", "name": device_name, "children": {}})
        
        processed_co_ids = set()
        for ch_id, channel in device.get("channels", {}).items():
            ch_name = get_best_channel_name(channel, str(ch_id))
            
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
        space_name = space.get("name") or "Unbenannter Bereich"
        space_id = space.get('identifier', space_name)
        space_node = parent_node["children"].setdefault(space_name, {"id": f"loc_{space_id}", "name": space_name, "children": {}})
        
        for pa in space.get("devices", []):
            device = devices.get(pa)
            if not device: continue
            
            dev_name = device.get('name') or 'Unbenannt'
            device_name = f"({pa}) {dev_name}"
            device_node = space_node["children"].setdefault(device_name, {"id": f"dev_{pa}", "name": device_name, "children": {}})
            
            processed_co_ids = set()
            for ch_id, channel in device.get("channels", {}).items():
                ch_name = get_best_channel_name(channel, str(ch_id))
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