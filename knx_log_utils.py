#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hilfsfunktionen zum Parsen und Cachen von KNX-Logdateien.
Wird von knx-lens.py importiert.
"""

import csv
import re
import logging
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

def parse_and_cache_log_data(lines: List[str], project_data: Dict) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, str]]]:
    """
    Parst die Log-Datei, aktualisiert das `payload_history` UND
    baut den `cached_log_data` Cache mit angereicherten Daten auf.
    
    Gibt (payload_history, cached_log_data) zurück.
    """
    # Lokale Dictionaries, die zurückgegeben werden
    payload_history: Dict[str, List[Dict[str, str]]] = {}
    cached_log_data: List[Dict[str, str]] = []
    
    first_content_lines = [line for line in lines[:20] if line.strip() and not line.strip().startswith("=")]
    log_format = detect_log_format(first_content_lines)
    if not log_format:
        logging.warning("Konnte Log-Format beim Parsen für Cache nicht bestimmen.")
        return payload_history, cached_log_data

    # Hole Dictionaries für schnellen Lookup
    devices_dict = project_data.get("devices", {})
    ga_dict = project_data.get("group_addresses", {})

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
                
                # 1. Payload History (NUR wenn Payload existiert)
                if payload is not None:
                    if ga not in payload_history:
                        payload_history[ga] = []
                    payload_history[ga].append({'timestamp': timestamp, 'payload': payload})
                
                # 2. Cache mit angereicherten Daten aufbauen (IMMER)
                pa_name = devices_dict.get(pa, {}).get("name", "N/A")
                ga_name = ga_dict.get(ga, {}).get("name", "N/A")
                
                cached_log_data.append({
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
    
    # Payload History sortieren
    for ga in payload_history:
        payload_history[ga].sort(key=lambda x: x['timestamp'])

    return payload_history, cached_log_data