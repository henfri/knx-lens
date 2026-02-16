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
from typing import Dict, List, Any, Optional

# SAFE IMPORT
try:
    from xknxproject import XKNXProj
except ImportError:
    XKNXProj = None

# ============================================================================
# CONSTANTS & TYPE DEFINITIONS
# ============================================================================

TreeData = Dict[str, Any]

# Cache Settings
CACHE_CHUNK_SIZE = 4096  # Bytes to read per iteration when computing MD5
CACHE_FILE_SUFFIX = ".cache.json"

# Project Data Keys
PROJECT_KEY_WRAPPER = "project"
PROJECT_KEY_MD5 = "md5"
PROJECT_KEY_DEVICES = "devices"
PROJECT_KEY_GROUP_ADDRESSES = "group_addresses"
PROJECT_KEY_BUILDING = "building_structure"
PROJECT_KEY_CHANNELS = "channels"

# Device/GA Field Names
FIELD_NAME = "name"
FIELD_TEXT = "text"
FIELD_FUNCTION_TEXT = "function_text"
FIELD_DESCRIPTION = "description"
FIELD_ADDRESS = "address"
FIELD_INDIVIDUAL_ADDRESS = "individual_address"
FIELD_DEVICE_ADDRESS = "device_address"
FIELD_NUMBER = "number"
FIELD_CHILDREN = "children"
FIELD_COMMUNICATION_OBJECTS = "communication_object_ids"

def get_md5_hash(file_path: str) -> str:
    """Compute MD5 hash of file for cache invalidation.
    
    Args:
        file_path: Path to file
        
    Returns:
        Hex string of MD5 hash
    """
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(CACHE_CHUNK_SIZE), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def load_or_parse_project(knxproj_path: str, password: Optional[str]) -> Dict:
    """Load KNX project from cache or parse from file.
    
    Uses MD5 hash for cache invalidation. Returns wrapped dict with 'project' key.
    
    Args:
        knxproj_path: Path to .knxproj file
        password: Optional password for encrypted project
        
    Returns:
        Dict with 'project' (parsed data) and 'md5' (hash) keys
    """
    if XKNXProj is None:
        logging.error("xknxproject ist nicht installiert.")
        return {}

    if not os.path.exists(knxproj_path):
        raise FileNotFoundError(f"Projektdatei nicht gefunden unter '{knxproj_path}'")
    
    cache_path = knxproj_path + CACHE_FILE_SUFFIX
    
    if os.path.exists(cache_path):
        try:
             with open(cache_path, 'r', encoding='utf-8') as f:
                current_md5 = get_md5_hash(knxproj_path)
                cache_data = json.load(f)
                if cache_data.get(PROJECT_KEY_MD5) == current_md5 and PROJECT_KEY_WRAPPER in cache_data:
                    logging.info(f"Projekt aus Cache geladen.")
                    return cache_data 
        except Exception as e:
            logging.warning(f"Cache-Fehler ({e}), parse neu.")

    logging.info(f"Parse KNX-Projektdatei: {knxproj_path}...")
    start_time = time.time()
    try:
        xknxproj = XKNXProj(knxproj_path, password=password)
        raw_parsed_data = xknxproj.parse()
        parse_time = time.time() - start_time
        logging.info(f"Projekt geparst in {parse_time:.2f}s")
        
        project_wrapper = {
            PROJECT_KEY_MD5: get_md5_hash(knxproj_path),
            PROJECT_KEY_WRAPPER: raw_parsed_data
        }
        
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(project_wrapper, f, indent=2)
        except Exception: pass

        return project_wrapper
    except Exception as e:
        logging.critical(f"Fehler beim Parsen: {e}")
        return {}

def get_best_name(data: Dict, default_name: str) -> str:
    """Extract best available name from data dict.
    
    Prefers: text > function_text, joined with ': '
    
    Args:
        data: Device/GA data dict
        default_name: Fallback if no name found
        
    Returns:
        Human-readable name string
    """
    if not isinstance(data, dict): 
        return default_name
    
    parts = []
    if val := data.get(FIELD_TEXT):
        parts.append(str(val).strip())
    if val := data.get(FIELD_FUNCTION_TEXT):
        parts.append(str(val).strip())
        
    if parts:
        return " - ".join(parts)
    
    if val := data.get("name"):
        return str(val).strip()
        
    return data.get("description") or default_name

def get_best_channel_name(channel: Dict, channel_id: str) -> str:
    """Get human-readable channel name with fallback.
    
    Args:
        channel: Channel data dict
        channel_id: Channel identifier for fallback
        
    Returns:
        Channel name string
    """
    return get_best_name(channel, f"Kanal {channel_id}")

def add_com_objects_to_node(node: TreeData, co_ids: List[str], project_wrapper: Dict) -> None:
    """Add communication objects to tree node with GA links.
    
    Resolves first GA link for each CO and appends to node payload display.
    Handles both wrapped and unwrapped project data.
    
    Args:
        node: Tree node dict to add children to
        co_ids: List of communication object IDs
        project_wrapper: Wrapped project data with 'project' key
    """
    if not co_ids: return
    
    if "project" in project_wrapper: 
        project_data = project_wrapper[PROJECT_KEY_WRAPPER]
    else: 
        project_data = project_wrapper

    com_objects = project_data.get("communication_objects", {})
    all_gas = project_data.get("group_addresses", {}) 
    
    for co_id in co_ids:
        co = com_objects.get(co_id)
        if not co: continue
        
        co_name = get_best_name(co, f"KO {co_id}")
        
        # Hole Verknüpfungen
        ga_links_raw = co.get("group_addresses", [])
        if not ga_links_raw:
            ga_links_raw = co.get("group_address_links", [])
            
        ga_links = []
        if isinstance(ga_links_raw, list):
            ga_links = [str(g) for g in ga_links_raw]
        elif isinstance(ga_links_raw, dict):
            ga_links = [str(g) for g in ga_links_raw.keys()]

        formatted_gas = []
        valid_ga_set = set()

        # --- FIX 2: Nur erster Name ---
        for i, ga_addr_str in enumerate(ga_links):
            valid_ga_set.add(ga_addr_str)
            
            if i == 0:
                # Erster Eintrag: Mit Name
                ga_obj = all_gas.get(ga_addr_str, {})
                ga_friendly_name = get_best_name(ga_obj, "")
                if ga_friendly_name:
                    formatted_gas.append(f"{ga_addr_str} ({ga_friendly_name})")
                else:
                    formatted_gas.append(ga_addr_str)
            else:
                # Weitere Einträge: Nur Nummer
                formatted_gas.append(ga_addr_str)
        
        if formatted_gas:
            ga_display_str = ", ".join(formatted_gas)
            ko_label = f"{co.get('number', '?')}: {co_name} -> [{ga_display_str}]"
        else:
            ko_label = f"{co.get('number', '?')}: {co_name}"
        
        node["children"][ko_label] = {
            "id": co_id, 
            "name": ko_label, 
            "data": {"type": "co", "original_name": ko_label, "gas": valid_ga_set},
            "children": {}
        }

def build_ga_tree_data(project: Dict) -> TreeData:
    if "project" in project: raw_data = project["project"]
    else: raw_data = project

    group_addresses = raw_data.get("group_addresses", {})
    group_ranges = raw_data.get("group_ranges", {})
    root_node = {"id": "ga_root", "name": "Gruppenadressen", "children": {}}
    hierarchy = {}

    for ga_id, ga in group_addresses.items():
        parts = ga.get("address", "").split('/')
        if len(parts) < 1: continue
        main_k = parts[0]
        if main_k not in hierarchy: hierarchy[main_k] = {"name": f"HG {main_k}", "subs": {}}
        if len(parts) > 1:
            sub_k = parts[1]
            if sub_k not in hierarchy[main_k]["subs"]: hierarchy[main_k]["subs"][sub_k] = {"name": f"MG {sub_k}", "gas": {}}
            hierarchy[main_k]["subs"][sub_k]["gas"][ga_id] = ga

    def flatten_ranges(ranges):
        flat = {}
        for k, v in ranges.items():
            flat[k] = v
            if "group_ranges" in v: flat.update(flatten_ranges(v["group_ranges"]))
        return flat
    
    flat_ranges = flatten_ranges(group_ranges)
    for addr, details in flat_ranges.items():
        parts = addr.split('/')
        name = details.get("name")
        if not name: continue
        if len(parts) == 1 and parts[0] in hierarchy: hierarchy[parts[0]]["name"] = name
        elif len(parts) == 2 and parts[0] in hierarchy and parts[1] in hierarchy[parts[0]]["subs"]:
            hierarchy[parts[0]]["subs"][parts[1]]["name"] = name

    for hg_key in sorted(hierarchy.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        hg_data = hierarchy[hg_key]
        hg_label = f"({hg_key}) {hg_data['name']}"
        hg_node = root_node["children"].setdefault(hg_label, {"id": f"hg_{hg_key}", "name": hg_label, "children": {}})
        for mg_key in sorted(hg_data["subs"].keys(), key=lambda x: int(x) if x.isdigit() else 0):
            mg_data = hg_data["subs"][mg_key]
            mg_label = f"({hg_key}/{mg_key}) {mg_data['name']}"
            mg_node = hg_node["children"].setdefault(mg_label, {"id": f"mg_{hg_key}_{mg_key}", "name": mg_label, "children": {}})
            sorted_gas = sorted(mg_data["gas"].items(), key=lambda x: x[1].get("address_int", 0))
            for ga_id, ga in sorted_gas:
                ga_name = get_best_name(ga, ga_id)
                label = f"({ga['address']}) {ga_name}"
                mg_node["children"][label] = {"id": ga_id, "name": label, "data": {"type": "ga", "gas": {ga.get("address")}, "original_name": label}, "children": {}}
    return root_node

def build_pa_tree_data(project: Dict) -> TreeData:
    if "project" in project: raw_data = project["project"]
    else: raw_data = project

    pa_tree = {"id": "pa_root", "name": "Geräte (Topologie)", "children": {}}
    devices = raw_data.get("devices", {})
    topology = raw_data.get("topology", {})
    
    area_names = {str(area['address']): (area.get('name') or '') for area in topology.get("areas", {}).values()}
    line_names = {}
    for area in topology.get("areas", {}).values():
        for line in area.get("lines", {}).values():
            line_id = f"{area['address']}.{line['address']}"
            line_names[line_id] = (line.get('name') or '')

    for pa in sorted(devices.keys()):
        device = devices[pa]
        parts = pa.split('.')
        if len(parts)!= 3: continue
        area_id, line_id, dev_id = parts
        
        area_lbl = f"Bereich {area_id}"
        if area_names.get(area_id): area_lbl = f"({area_id}) {area_names[area_id]}"
        area_node = pa_tree["children"].setdefault(area_lbl, {"id": f"a_{area_id}", "name": area_lbl, "children": {}})

        full_line_id = f"{area_id}.{line_id}"
        line_lbl = f"Linie {full_line_id}"
        if line_names.get(full_line_id): line_lbl = f"({full_line_id}) {line_names[full_line_id]}"
        line_node = area_node["children"].setdefault(line_lbl, {"id": f"l_{full_line_id}", "name": line_lbl, "children": {}})

        dev_name = get_best_name(device, 'Unnamed')
        device_node = line_node["children"].setdefault(f"({pa}) {dev_name}", {"id": f"dev_{pa}", "name": f"({pa}) {dev_name}", "children": {}})

        processed_co_ids = set()
        for ch_id, channel in device.get("channels", {}).items():
            ch_name = get_best_channel_name(channel, str(ch_id))
            ch_node = device_node["children"].setdefault(ch_name, {"id": f"ch_{pa}_{ch_id}", "name": ch_name, "children": {}})
            
            # HIER IST DIE ÄNDERUNG: co_ids statt co_ids_in_channel für Konsistenz
            co_ids = channel.get("communication_object_ids", [])
            add_com_objects_to_node(ch_node, co_ids, project)
            processed_co_ids.update(co_ids)
        
        all_co_ids = set(device.get("communication_object_ids", []))
        rem_ids = all_co_ids - processed_co_ids
        if rem_ids: 
            # Sort remaining CO IDs for consistent, predictable ordering by CO number
            if "project" in project: project_data = project["project"]
            else: project_data = project
            com_objects_dict = project_data.get("communication_objects", {})
            sorted_rem_ids = sorted(rem_ids, key=lambda x: com_objects_dict.get(x, {}).get('number', 0))
            add_com_objects_to_node(device_node, sorted_rem_ids, project)
            
    return pa_tree

def build_building_tree_data(project: Dict) -> TreeData:
    if "project" in project: raw_data = project["project"]
    else: raw_data = project

    locations = raw_data.get("locations", {})
    devices = raw_data.get("devices", {})
    building_tree = {"id": "bldg_root", "name": "Gebäude", "children": {}}

    def process_space(space: Any, parent_node: Dict):
        if not isinstance(space, dict): return

        space_name = get_best_name(space, "Unnamed Area")
        space_id = space.get('identifier', space_name)
        space_node = parent_node["children"].setdefault(space_name, {"id": f"loc_{space_id}", "name": space_name, "children": {}})

        devs = space.get("devices", [])
        if isinstance(devs, dict): devs = list(devs.keys())

        if isinstance(devs, list):
            for pa in devs:
                if not isinstance(pa, str): continue
                device = devices.get(pa)
                if not device: continue

                dev_name = get_best_name(device, 'Unnamed')
                device_name = f"({pa}) {dev_name}"
                device_node = space_node["children"].setdefault(device_name, {"id": f"dev_{pa}", "name": device_name, "children": {}})

                processed_co_ids = set()
                for ch_id, channel in device.get("channels", {}).items():
                    ch_name = get_best_channel_name(channel, str(ch_id))
                    ch_node = device_node["children"].setdefault(ch_name, {"id": f"ch_{pa}_{ch_id}", "name": ch_name, "children": {}})

                    # --- FIX: Variable eindeutig benannt (co_ids), um NameError zu verhindern ---
                    co_ids = channel.get("communication_object_ids", [])
                    add_com_objects_to_node(ch_node, co_ids, project)
                    processed_co_ids.update(co_ids)

                all_co_ids = set(device.get("communication_object_ids", []))
                rem_ids = all_co_ids - processed_co_ids
                if rem_ids:
                    # Sort remaining CO IDs for consistent, predictable ordering by CO number
                    com_objects_dict = raw_data.get("communication_objects", {})
                    sorted_rem_ids = sorted(rem_ids, key=lambda x: com_objects_dict.get(x, {}).get('number', 0))
                    add_com_objects_to_node(device_node, sorted_rem_ids, project)

        sub_spaces = space.get("spaces", {})
        if isinstance(sub_spaces, dict):
             for child in sub_spaces.values(): process_space(child, space_node)
        elif isinstance(sub_spaces, list):
             for child in sub_spaces: process_space(child, space_node)

    if isinstance(locations, dict):
        for location in locations.values(): process_space(location, building_tree)
    elif isinstance(locations, list):
        for location in locations: process_space(location, building_tree)
            
    return building_tree