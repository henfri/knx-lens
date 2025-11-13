#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hilfsfunktionen zum Parsen und Cachen von KNX-Logdateien.
Wird von knx-lens.py importiert.
"""

import csv
import re
import logging
from datetime import datetime, time as datetime_time
from typing import Dict, List, Any, Optional, Tuple

def detect_log_format(first_lines: List[str]) -> Optional[str]:
    """Erkennt das Format einer Log-Datei (pipe oder csv)."""
    for line in first_lines:
        line = line.strip()
        if not line or line.startswith("="): continue
        if ' | ' in line and len(line.split('|')) > 4 and re.search(r'\d+/\d+/\d+', line.split('|')[3]):
            return 'pipe_separated'
        if ';' in line:
            return 'csv'
    return None

def _parse_lines_internal(
    lines: List[str], 
    project_data: Dict, 
    log_format: str,
    time_filter_start: Optional[datetime_time] = None, 
    time_filter_end: Optional[datetime_time] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """
    Interne Parsing-Engine. 
    Gibt (neue_payload_einträge, neue_cache_einträge) zurück.
    """
    
    new_payload_items = []
    new_cached_items = []
    
    devices_dict = project_data.get("devices", {})
    ga_dict = project_data.get("group_addresses", {})
    
    has_time_filter = time_filter_start or time_filter_end

    for line in lines:
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("="): continue
        
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
                
                if has_time_filter:
                    try:
                        time_str = timestamp.split(' ')[1].split('.')[0]
                        log_time = datetime.strptime(time_str, "%H:%M:%S").time()
                        
                        if time_filter_start and log_time < time_filter_start:
                            continue 
                        if time_filter_end and log_time > time_filter_end:
                            continue
                            
                    except (ValueError, IndexError):
                        logging.debug(f"Konnte Timestamp für Zeitfilter nicht parsen: {timestamp}")
                        continue

                if payload is not None:
                    new_payload_items.append({
                        "ga": ga,
                        "timestamp": timestamp,
                        "payload": payload
                    })
                
                pa_name = devices_dict.get(pa, {}).get("name", "N/A")
                ga_name = ga_dict.get(ga, {}).get("name", "N/A")
                
                new_cached_items.append({
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
            
    return new_payload_items, new_cached_items

def parse_and_cache_log_data(
    lines: List[str], 
    project_data: Dict, 
    time_filter_start: Optional[datetime_time] = None, 
    time_filter_end: Optional[datetime_time] = None
) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, str]]]:
    """
    [VOLLSTÄNDIGER RELOAD]
    Parst die Log-Datei, baut das `payload_history` UND
    den `cached_log_data` Cache komplett neu auf.
    Gibt (payload_history, cached_log_data) zurück.
    """
    payload_history: Dict[str, List[Dict[str, str]]] = {}
    cached_log_data: List[Dict[str, str]] = []
    
    first_content_lines = [line for line in lines[:20] if line.strip() and not line.strip().startswith("=")]
    log_format = detect_log_format(first_content_lines)
    if not log_format:
        logging.warning("Konnte Log-Format beim Parsen für Cache nicht bestimmen.")
        return payload_history, cached_log_data

    new_payload_items, new_cached_items = _parse_lines_internal(
        lines, project_data, log_format, time_filter_start, time_filter_end
    )
    
    cached_log_data = new_cached_items
    for item in new_payload_items:
        ga = item["ga"]
        if ga not in payload_history:
            payload_history[ga] = []
        payload_history[ga].append({'timestamp': item["timestamp"], 'payload': item["payload"]})

    for ga in payload_history:
        payload_history[ga].sort(key=lambda x: x['timestamp'])

    return payload_history, cached_log_data

# --- FUNKTION GEÄNDERT: GIBT JETZT NEUE ZEILEN ZURÜCK ---
def append_new_log_lines(
    lines: List[str], 
    project_data: Dict, 
    payload_history: Dict[str, List[Dict[str, str]]],
    cached_log_data: List[Dict[str, str]],
    time_filter_start: Optional[datetime_time] = None, 
    time_filter_end: Optional[datetime_time] = None
) -> List[Dict[str, str]]: # <-- Rückgabetyp geändert
    """
    [DELTA-RELOAD]
    Parst nur neue Zeilen, hängt sie an die Listen an UND
    GIBT die neuen Cache-Einträge zurück.
    """
    
    log_format = detect_log_format(lines[:20])
    if not log_format:
        if cached_log_data:
            first_entry = cached_log_data[0]
            simulated_line = f"{first_entry['timestamp']} | {first_entry['pa']} | | {first_entry['ga']} | | {first_entry['payload']}"
            log_format = detect_log_format([simulated_line])
    if not log_format:
        logging.warning("Konnte Log-Format für Delta-Update nicht bestimmen.")
        return [] # Leere Liste zurückgeben

    new_payload_items, new_cached_items = _parse_lines_internal(
        lines, project_data, log_format, time_filter_start, time_filter_end
    )
    
    cached_log_data.extend(new_cached_items)
    for item in new_payload_items:
        ga = item["ga"]
        if ga not in payload_history:
            payload_history[ga] = []
        payload_history[ga].append({'timestamp': item["timestamp"], 'payload': item["payload"]})
    
    return new_cached_items # <-- Die neuen Zeilen zurückgeben