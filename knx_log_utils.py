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

# ============================================================================
# CONSTANTS
# ============================================================================

# Pre-compiled regex for performance
GA_PATTERN = re.compile(r'\d+/\d+/\d+')

# Log Format Detection
LOG_FORMAT_PIPE_SEPARATED = 'pipe_separated'
LOG_FORMAT_CSV = 'csv'
PIPE_SEPARATOR = ' | '
CSV_DELIMITER = ';'
MIN_LOG_FORMAT_CHECK_LINES = 20

# Timestamp Parsing
TIMESTAMP_TIME_FORMAT = "%H:%M:%S"

def detect_log_format(first_lines: List[str]) -> Optional[str]:
    """Detect log file format (pipe-separated or CSV).
    
    Args:
        first_lines: First N lines from log file
        
    Returns:
        Format string ('pipe_separated', 'csv') or None if unrecognized
    """
    for line in first_lines:
        line = line.strip()
        if not line or line.startswith("="): continue
        if PIPE_SEPARATOR in line and len(line.split('|')) > 4 and GA_PATTERN.search(line.split('|')[3]):
            return LOG_FORMAT_PIPE_SEPARATED
        if CSV_DELIMITER in line:
            return LOG_FORMAT_CSV
    return None

def _parse_lines_internal(
    lines: List[str], 
    project_data: Dict, 
    log_format: str,
    time_filter_start: Optional[datetime_time] = None, 
    time_filter_end: Optional[datetime_time] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Internal parsing engine for log lines.
    
    Parses log lines based on format and extracts timestamp, GA, payload.
    Supports optional time filtering.
    
    Args:
        lines: Log lines to parse
        project_data: Loaded project data (wrapped or unwrapped)
        log_format: Format type ('pipe_separated' or 'csv')
        time_filter_start: Optional start time filter
        time_filter_end: Optional end time filter
        
    Returns:
        Tuple of (payload_items, cached_items) dicts
    """
    
    new_payload_items = []
    new_cached_items = []
    
    # --- FIX für N/A Problem: Wrapper entpacken ---
    if "project" in project_data:
        actual_data = project_data["project"]
    else:
        actual_data = project_data

    devices_dict = actual_data.get("devices", {})
    ga_dict = actual_data.get("group_addresses", {})
    # ----------------------------------------------
    
    has_time_filter = time_filter_start or time_filter_end

    for line in lines:
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("="): continue
        
        try:
            timestamp, pa, ga, payload = None, "N/A", None, None
            
            if log_format == LOG_FORMAT_PIPE_SEPARATED:
                parts = [p.strip() for p in clean_line.split('|')]
                if len(parts) > 3:
                    timestamp = parts[0]
                    ga = parts[3]
                    pa = parts[1] if len(parts) > 1 else "N/A"
                    payload = parts[5] if len(parts) > 5 else None
            
            elif log_format == LOG_FORMAT_CSV:
                row = next(csv.reader([clean_line], delimiter=';'))
                if len(row) > 4:
                    timestamp = row[0]
                    ga = row[4]
                    pa = row[1] if len(row) > 1 else "N/A"
                    payload = row[6] if len(row) > 6 else None
            
            if timestamp and ga and GA_PATTERN.match(ga):
                
                if has_time_filter:
                    try:
                        time_str = timestamp.split(' ')[1].split('.')[0]
                        log_time = datetime.strptime(time_str, TIMESTAMP_TIME_FORMAT).time()
                        
                        if time_filter_start and log_time < time_filter_start:
                            continue 
                        if time_filter_end and log_time > time_filter_end:
                            continue
                            
                    except (ValueError, IndexError):
                        logging.debug(f"Konnte Timestamp für Zeitfilter nicht parsen: {timestamp}")
                        continue

                payload_str = payload if payload is not None else "N/A"
                
                if payload is not None:
                    new_payload_items.append({
                        "ga": ga,
                        "timestamp": timestamp,
                        "payload": payload_str
                    })
                
                pa_name = devices_dict.get(pa, {}).get("name", "N/A")
                ga_name = ga_dict.get(ga, {}).get("name", "N/A")
                
                search_string = (
                    f"{timestamp} "
                    f"{pa} "
                    f"{pa_name} "
                    f"{ga} "
                    f"{ga_name} "
                    f"{payload_str}"
                )
                
                new_cached_items.append({
                    "timestamp": timestamp,
                    "pa": pa,
                    "pa_name": pa_name,
                    "ga": ga,
                    "ga_name": ga_name,
                    "payload": payload_str,
                    "search_string": search_string
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
    """Parse log file and build payload history + cache.
    
    Args:
        lines: All log lines from file
        project_data: Loaded project data
        time_filter_start: Optional start time filter
        time_filter_end: Optional end time filter
        
    Returns:
        Tuple of (payload_history dict, cached_log_data list)
    """
    payload_history: Dict[str, List[Dict[str, str]]] = {}
    cached_log_data: List[Dict[str, str]] = []
    
    first_content_lines = [line for line in lines[:MIN_LOG_FORMAT_CHECK_LINES] if line.strip() and not line.strip().startswith("=")]
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

def append_new_log_lines(
    lines: List[str], 
    project_data: Dict, 
    payload_history: Dict[str, List[Dict[str, str]]],
    cached_log_data: List[Dict[str, str]],
    time_filter_start: Optional[datetime_time] = None, 
    time_filter_end: Optional[datetime_time] = None
) -> List[Dict[str, str]]:
    """Append new log lines to existing cache and payload history.
    
    Args:
        lines: New log lines to append
        project_data: Loaded project data
        payload_history: Existing payload history (modified in place)
        cached_log_data: Existing cache (modified in place)
        time_filter_start: Optional start time filter
        time_filter_end: Optional end time filter
        
    Returns:
        List of newly added cache items
    """
    log_format = detect_log_format(lines[:MIN_LOG_FORMAT_CHECK_LINES])
    if not log_format:
        if cached_log_data:
            first_entry = cached_log_data[0]
            simulated_line = f"{first_entry['timestamp']} | {first_entry['pa']} | | {first_entry['ga']} | | {first_entry['payload']}"
            log_format = detect_log_format([simulated_line])
    if not log_format:
        logging.warning("Konnte Log-Format für Delta-Update nicht bestimmen.")
        return [] 

    new_payload_items, new_cached_items = _parse_lines_internal(
        lines, project_data, log_format, time_filter_start, time_filter_end
    )
    
    cached_log_data.extend(new_cached_items)
    for item in new_payload_items:
        ga = item["ga"]
        if ga not in payload_history:
            payload_history[ga] = []
        payload_history[ga].append({'timestamp': item["timestamp"], 'payload': item["payload"]})
    
    return new_cached_items