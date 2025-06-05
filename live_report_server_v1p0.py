#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import hjson
import os
import shutil
import subprocess
import threading
import uuid
import time
import copy
import re
from flask import render_template, request, jsonify, send_from_directory, Blueprint, Flask, current_app
from flask_cors import CORS # Added CORS import
from bs4 import BeautifulSoup # Added BeautifulSoup import
# Removed: from .app import app as main_flask_app
import os.path # For path manipulation in update_html_report_on_disk
# Attempt to import database models and extension, fail gracefully if not available for standalone use
try:
    from models import Repo
    from extensions import db
except ImportError:
    Repo = None
    db = None
    print("Warning: 'models' or 'extensions' module not found. Database features will be disabled.")

_current_file_dir = os.path.dirname(os.path.abspath(__file__))
_project_root_approx = os.path.dirname(_current_file_dir)
base_dir_for_templates = os.path.join(_project_root_approx, 'templates')

bp = Blueprint('live_reporter', __name__, template_folder=base_dir_for_templates)

@bp.before_request
def log_bp_request():
    _logger_instance_bp = getattr(bp, 'logger', None)
    if hasattr(request, 'getBluePrintAppLogger'):
        _logger_instance_bp = request.getBluePrintAppLogger()
    elif current_app:
        _logger_instance_bp = current_app.logger

    if not _logger_instance_bp:
        print("Warning (bp.before_request): No Flask logger found. Using print fallback for bp.before_request.")
        class PrintLoggerBp:
            def info(self, msg): print(f"INFO (bp.before_request_fallback): {msg}")
            def warning(self, msg): print(f"WARN (bp.before_request_fallback): {msg}")
            def error(self, msg, exc_info=False):
                print(f"ERROR (bp.before_request_fallback): {msg}")
                if exc_info: import traceback; traceback.print_exc()
        _logger_instance_bp = PrintLoggerBp()

    _logger_instance_bp.info(f"--- Blueprint 'live_reporter' before_request: Path: {request.path}, Endpoint: {request.endpoint}, Method: {request.method} ---")
    print(f"[DEBUG_PRINT_BP_BEFORE_REQUEST] Path: {request.path}, Endpoint: {request.endpoint}, Method: {request.method} at {time.strftime('%Y-%m-%d %H:%M:%S')}")

script_dir = os.path.dirname(os.path.abspath(__file__))
JOB_STATUS = {}

def update_job_status(job_id, status, message=None, command=None, returncode=None, stdout=None, stderr=None):
    if job_id not in JOB_STATUS:
        JOB_STATUS[job_id] = {"output_lines": [], "status": "initializing", "message": "Job initializing."}

    JOB_STATUS[job_id]['status'] = status
    if message is not None: JOB_STATUS[job_id]['message'] = message
    if command is not None: JOB_STATUS[job_id]['command'] = command
    if returncode is not None: JOB_STATUS[job_id]['returncode'] = returncode
    if stdout is not None: JOB_STATUS[job_id]['stdout'] = stdout
    if stderr is not None: JOB_STATUS[job_id]['stderr'] = stderr

def add_output_line_to_job(job_id, line):
    if job_id not in JOB_STATUS:
        JOB_STATUS[job_id] = {"output_lines": [], "status": "unknown", "message": "Job initialized by output line."}
    elif "output_lines" not in JOB_STATUS[job_id]:
         JOB_STATUS[job_id]["output_lines"] = []
    JOB_STATUS[job_id]["output_lines"].append(line)

    if 'progress_summary' in JOB_STATUS[job_id] and JOB_STATUS[job_id].get('status') == 'running_msim':
        uvm_test_done_pattern = re.compile(r"\[TEST_DONE\]\s*Test\s*([\w_.-]+seed\d+)\s*\((\w+)\)")
        match = uvm_test_done_pattern.search(line)
        if match:
            status_from_log = match.group(2).upper()
            summary = JOB_STATUS[job_id]['progress_summary']
            if summary['processed_count'] < summary['total_selected']:
                summary['processed_count'] += 1
                if status_from_log == "PASSED":
                    summary['passed_count'] += 1
                elif status_from_log == "FAILED":
                    summary['failed_count'] += 1

def get_job_status(job_id):
    return JOB_STATUS.get(job_id, {"status": "not_found", "message": "Job ID not found.", "output_lines": []})

def get_project_root_from_branch_path(branch_path, job_id_for_logging=None):
    if not branch_path or '/work/' not in branch_path:
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: branchPath '{branch_path}' is invalid or does not contain '/work/'. Cannot determine project root.")
        return None
    project_root = branch_path.split('/work/', 1)[0]
    if job_id_for_logging:
        add_output_line_to_job(job_id_for_logging, f"Derived project root for icenv and HJSON base: {project_root} from branchPath: {branch_path}")
    return project_root

def _parse_individual_parse_run_log(parse_run_log_path):
    try:
        with open(parse_run_log_path, 'r', encoding='utf-8', errors='replace') as f:
            first_line = f.readline().strip().lower()
            if 'run.log passed' in first_line: return 'PASSED'
            elif 'run.log failed' in first_line or 'run.log is unknown' in first_line: return 'FAILED'
            else: return 'UNKNOWN'
    except FileNotFoundError: return None
    except Exception as e:
        print(f"Error parsing log file {parse_run_log_path}: {e}")
        return 'UNKNOWN'

def find_primary_log_for_rerun(base_search_path):
    if not base_search_path or not os.path.isdir(base_search_path):
        print(f"Warning: Base search path for logs is invalid or not a directory: {base_search_path}")
        return None
    log_filenames_priority = ["run.log", "comp.log"]
    common_subdirs = ["", "latest"]
    for log_filename in log_filenames_priority:
        for subdir in common_subdirs:
            potential_log_path = os.path.join(base_search_path, subdir, log_filename)
            if os.path.exists(potential_log_path):
                print(f"Found log '{log_filename}' in common location: {os.path.abspath(potential_log_path)}")
                return os.path.abspath(potential_log_path)
        print(f"Log '{log_filename}' not in common locations. Starting deeper search in {base_search_path}...")
        for root, _, files in os.walk(base_search_path):
            if log_filename in files:
                found_path = os.path.abspath(os.path.join(root, log_filename))
                print(f"Found log '{log_filename}' via os.walk: {found_path}")
                return found_path
        print(f"Log '{log_filename}' not found via os.walk in {base_search_path}.")
    print(f"No run.log or comp.log found in {base_search_path} after checking common locations and deep search.")
    return None

def parse_msim_output_for_test_statuses(msim_stdout, selected_cases_with_seed, actual_sim_root_for_parsing, base_log_path_for_html, job_id_for_logging=None):
    results_map = {}
    if job_id_for_logging:
        add_output_line_to_job(job_id_for_logging, f"Starting detailed status parsing. Sim root for parsing: {actual_sim_root_for_parsing}, Base HTML log path: {base_log_path_for_html}")
    if not base_log_path_for_html and actual_sim_root_for_parsing:
         add_output_line_to_job(job_id_for_logging, "Warning: base_log_path_for_html is missing, HTML log paths might be incorrect.")

    for case_id in selected_cases_with_seed:
        current_status = "UNKNOWN"; error_hint = "Status not determined."
        safe_base_html_path = base_log_path_for_html.replace(os.sep, '/') if base_log_path_for_html else "unknown_html_base"
        html_log_path = f"{safe_base_html_path}/sim/{case_id}/latest/run.log"
        case_id_variant_dir_name = None; individual_test_sim_dir_actual = None

        if actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
            try:
                found_match = False; dir_items = sorted(os.listdir(actual_sim_root_for_parsing))
                for item_name in dir_items:
                    item_path = os.path.join(actual_sim_root_for_parsing, item_name)
                    if os.path.isdir(item_path) and item_name.startswith(case_id):
                        case_id_variant_dir_name = item_name; individual_test_sim_dir_actual = item_path
                        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Found matching sim directory: {case_id_variant_dir_name} at {individual_test_sim_dir_actual}")
                        found_match = True; break
                if not found_match and job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: No directory starting with '{case_id}' found in '{actual_sim_root_for_parsing}'.")
            except Exception as e:
                if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Error listing or processing sim dirs in '{actual_sim_root_for_parsing}': {e}")

            if individual_test_sim_dir_actual and os.path.isdir(individual_test_sim_dir_actual):
                latest_log_dir = os.path.join(individual_test_sim_dir_actual, 'latest')
                if not os.path.isdir(latest_log_dir):
                    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): 'latest' symlink not found. Searching for newest timestamped directory...")
                    subdirs = [d for d in os.listdir(individual_test_sim_dir_actual) if os.path.isdir(os.path.join(individual_test_sim_dir_actual, d))]
                    if subdirs:
                        timestamped_subdirs = [os.path.join(individual_test_sim_dir_actual, d) for d in subdirs]
                        if timestamped_subdirs:
                            latest_log_dir = max(timestamped_subdirs, key=os.path.getmtime)
                            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): Found newest timestamped dir: {os.path.basename(latest_log_dir)}")
                        else: latest_log_dir = None
                    else: latest_log_dir = None

                if latest_log_dir and os.path.isdir(latest_log_dir):
                    parse_run_log_path = os.path.join(latest_log_dir, 'parse_run.log')
                    timestamp_or_latest_name = os.path.basename(latest_log_dir)
                    html_log_path = f"{safe_base_html_path}/sim/{case_id_variant_dir_name.replace(os.sep, '/')}/{timestamp_or_latest_name.replace(os.sep, '/')}/run.log"
                    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): Checking parse_run.log at {parse_run_log_path}")
                    status_from_parse_log = _parse_individual_parse_run_log(parse_run_log_path)
                    if status_from_parse_log:
                        current_status = status_from_parse_log
                        error_hint = "Failed (from parse_run.log)" if current_status == "FAILED" else ("" if current_status == "PASSED" else "Status unclear from parse_run.log")
                        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from parse_run.log: {current_status}")
                elif job_id_for_logging:
                    add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name if case_id_variant_dir_name else 'N/A'}): 'latest' or timestamped log directory not resolved or not a directory: {latest_log_dir}")
            elif job_id_for_logging and actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
                 add_output_line_to_job(job_id_for_logging, f"For {case_id}: Could not find or access specific simulation directory for this case variant.")
        elif job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Main sim root for parsing ('{actual_sim_root_for_parsing}') is not valid. Skipping individual log check.")

        if current_status == "UNKNOWN":
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: parse_run.log status is UNKNOWN or file not found. Checking msim_stdout.")
            uvm_test_done_pattern_specific = re.compile(r"\[TEST_DONE\]\s*Test\s*" + re.escape(case_id) + r"\s*\((\w+)\)")
            for line in msim_stdout.splitlines():
                match = uvm_test_done_pattern_specific.search(line)
                if match:
                    status_from_stdout = match.group(1).upper()
                    current_status = status_from_stdout
                    error_hint = "Failed (from [TEST_DONE] in msim stdout)" if status_from_stdout == "FAILED" else ("" if status_from_stdout == "PASSED" else error_hint)
                    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from msim_stdout [TEST_DONE]: {current_status}")
                    break
        results_map[case_id] = {"id": case_id, "status": current_status, "error_hint": error_hint, "new_log_path": html_log_path}
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Final determined status: {current_status}, Log: {html_log_path}")
    return list(results_map.values())

def prepare_rerun_hjson_files(project_root_for_hjson, options, temp_rerun_dir, ip_name):
    print(f"--- prepare_rerun_hjson_files called for IP: {ip_name} ---")
    print(f"Using project_root_for_hjson: {project_root_for_hjson}")
    job_id_for_logging = options.get("job_id_for_logging")
    proj_root_dir = project_root_for_hjson
    if not proj_root_dir:
        error_msg = "CRITICAL ERROR: Project root directory was not provided or derived. Cannot locate original HJSON files."
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, "Error: Project root directory not available. Configure server environment or check branchPath.")
        return None

    base_search_dir_for_hjson = os.path.join(proj_root_dir, "dv", "sim_ctrl", "ts")
    target_hjson_filename = f"{ip_name}.hjson"; found_original_hjson_path = None
    log_msg_search = f"Searching for '{target_hjson_filename}' in '{base_search_dir_for_hjson}' and its subdirectories..."
    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, log_msg_search)
    else: print(log_msg_search)

    for root, dirs, files in os.walk(base_search_dir_for_hjson):
        if target_hjson_filename in files:
            found_original_hjson_path = os.path.join(root, target_hjson_filename)
            log_msg_found = f"Found source HJSON at: {found_original_hjson_path}"
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, log_msg_found)
            else: print(log_msg_found)
            break

    if not found_original_hjson_path:
        error_msg = f"CRITICAL ERROR: Source HJSON file '{target_hjson_filename}' not found in '{base_search_dir_for_hjson}' or its subdirectories."
        print(error_msg);
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, error_msg)
        return None

    target_hjson_dir_for_temp_copy = os.path.join(proj_root_dir, "dv", "sim_ctrl", "ts", "temp")
    print(f"Target directory for 'rerun.hjson' (temp copy) under PRJ_ICDIR: {target_hjson_dir_for_temp_copy}")
    try:
        os.makedirs(target_hjson_dir_for_temp_copy, exist_ok=True)
        print(f"Ensured target directory for 'rerun.hjson' (temp copy) exists: {target_hjson_dir_for_temp_copy}")
    except Exception as e:
        error_msg = f"CRITICAL ERROR: Failed to create target directory {target_hjson_dir_for_temp_copy} for 'rerun.hjson': {e}"
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to create target directory {target_hjson_dir_for_temp_copy}: {e}")
        return None

    temp_target_hjson_path = os.path.join(target_hjson_dir_for_temp_copy, "rerun.hjson")
    print(f"Temporary target HJSON path for modified copy: {temp_target_hjson_path}")
    try:
        shutil.copy(found_original_hjson_path, temp_target_hjson_path)
        print(f"Successfully copied '{found_original_hjson_path}' to '{temp_target_hjson_path}' for modification.")
    except Exception as e:
        error_msg = f"Error: Could not copy HJSON file from '{found_original_hjson_path}' to '{temp_target_hjson_path}': {e}"
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to copy HJSON: {e}")
        return None

    try:
        with open(temp_target_hjson_path, 'r') as file: target_hjson_data = hjson.load(file)
        print(f"Successfully loaded HJSON data from {temp_target_hjson_path}")
    except Exception as e:
        error_msg = f"Error: Could not read or parse HJSON from {temp_target_hjson_path}: {e}"
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to parse HJSON {temp_target_hjson_path}: {e}")
        return None

    final_tests_section_for_output = []; test_names_for_regression_list = []
    original_tests_from_hjson = target_hjson_data.get("tests", [])
    original_test_defs_map_by_base_name = {}
    # ... (HJSON modification logic as before) ...
    if isinstance(original_tests_from_hjson, list):
        # ...
        for test_def in original_tests_from_hjson:
            if isinstance(test_def, dict) and "name" in test_def: original_test_defs_map_by_base_name[test_def["name"]] = test_def
            # ...
    elif isinstance(original_tests_from_hjson, dict):
        # ...
        for base_name, test_def in original_tests_from_hjson.items():
            if isinstance(test_def, dict): original_test_defs_map_by_base_name[base_name] = test_def
            # ...
    # ...
    selected_cases_for_this_ip = [case_id for case_id in options.get('selectedCases', []) if case_id.startswith(ip_name + "_")]
    if not selected_cases_for_this_ip:
        print(f"Info: No cases selected for IP '{ip_name}'. 'tests' section in rerun.hjson will be empty.")
    else:
        for case_id_with_seed in selected_cases_for_this_ip:
            # ... (parsing case_id_with_seed, creating new_test_def_object, updating run_opts) ...
            parts = case_id_with_seed.split("_seed")
            if len(parts) != 2: print(f"Warning: Could not parse base name and seed from '{case_id_with_seed}'. Skipping this case for HJSON."); continue
            base_test_name, seed_str = parts[0], parts[1]
            try: seed_val = int(seed_str)
            except ValueError: print(f"Warning: Invalid seed value '{seed_str}' in '{case_id_with_seed}'. Skipping this case for HJSON."); continue
            original_def_template = original_test_defs_map_by_base_name.get(base_test_name)
            new_test_def_object = copy.deepcopy(original_def_template) if original_def_template else {"uvm_test_seq": f"unknown_vseq_for_{base_test_name}", "build_mode": f"unknown_build_mode_for_{base_test_name}"}
            if not original_def_template: print(f"Warning: Original definition template for base test '{base_test_name}' not found. Creating a minimal definition for rerun.")
            new_test_def_object['name'] = case_id_with_seed
            if 'seed' in new_test_def_object: del new_test_def_object['seed']
            current_run_opts = new_test_def_object.get("run_opts", [])
            if not isinstance(current_run_opts, list): current_run_opts = []
            updated_run_opts = [opt for opt in current_run_opts if not str(opt).startswith("+ntb_random_seed=")]
            updated_run_opts.append(f"+ntb_random_seed={seed_val}")
            new_test_def_object['run_opts'] = updated_run_opts
            final_tests_section_for_output.append(new_test_def_object)
            test_names_for_regression_list.append(case_id_with_seed)

    target_hjson_data['tests'] = final_tests_section_for_output
    rerun_regression_group = {"name": "rerun", "tests": test_names_for_regression_list}
    if not isinstance(target_hjson_data.get("regressions"), list):
        target_hjson_data["regressions"] = [rerun_regression_group]
    else:
        # ... (update or append rerun_regression_group) ...
        existing_rerun_index = next((i for i, reg in enumerate(target_hjson_data["regressions"]) if isinstance(reg, dict) and reg.get("name") == "rerun"), None)
        if existing_rerun_index is not None: target_hjson_data["regressions"][existing_rerun_index] = rerun_regression_group
        else: target_hjson_data["regressions"].append(rerun_regression_group)
    try:
        with open(temp_target_hjson_path, 'w') as file: hjson.dump(target_hjson_data, file, indent=2)
        print(f"Successfully wrote modified HJSON to {temp_target_hjson_path}")
        return temp_target_hjson_path
    except Exception as e:
        # ... (error handling) ...
        error_msg = f"Error: Could not write modified HJSON to {temp_target_hjson_path}: {e}"; print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to write HJSON {temp_target_hjson_path}: {e}")
        return None

def calculate_total_stats_from_html(html_file_path, job_id_for_logging=None, logger_for_internal_errors=None):
    """
    Parses an HTML report file to calculate total test statistics.
    """
    stats = {'total': 0, 'passed': 0, 'failed': 0, 'killed': 0, 'other': 0, 'pass_rate': 0.0}
    actual_logger = logger_for_internal_errors if logger_for_internal_errors else current_app.logger # Fallback
    
    log_func = lambda msg, level="info": (actual_logger.info(msg) if level == "info" else actual_logger.error(msg)) if actual_logger else print(msg)

    if not html_file_path or not os.path.exists(html_file_path):
        error_msg = f"Error: HTML file path not provided or does not exist for stat calculation: {html_file_path}"
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, error_msg)
        log_func(error_msg, "error")
        return None
        
    try:
        with open(html_file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
        
        table_body = soup.find('table', id='detailedStatusTable').find('tbody')
        if not table_body:
            error_msg = f"Error: Could not find table body (tbody with id='detailedStatusTable') in HTML for total stats: {html_file_path}"
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, error_msg)
            log_func(error_msg, "error")
            return None

        all_rows_in_tbody = table_body.find_all('tr', recursive=False) # Get only direct children <tr>
        num_total_rows = len(all_rows_in_tbody)
        rows_to_process = []

        if num_total_rows == 0:
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, "Debug_HTML_Stats: No rows found in tbody.")
            # rows_to_process remains empty
        elif num_total_rows == 1: # If only 1 row, process it (could be data or the footer itself)
            rows_to_process = all_rows_in_tbody
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Processing the only row (1 total) found in tbody.")
        else: # num_total_rows > 1, skip only the LAST row
            rows_to_process = all_rows_in_tbody[:-1] # Process all rows except the last one
            if job_id_for_logging: 
                add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Skipping only the last row. Processing {len(rows_to_process)} of {num_total_rows} total rows.")
                last_row_text = all_rows_in_tbody[-1].get_text(strip=True, separator='|')[:100]
                add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Skipped last row content: {last_row_text}...")
                # No first row is explicitly skipped here if num_total_rows > 1

        for row_idx, row in enumerate(rows_to_process):
            td_cells = row.find_all('td')

            if not td_cells:
                if job_id_for_logging:
                    row_content_for_log = row.get_text(strip=True, separator='|')[:70]
                    add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Row {row_idx} (in selection) has no <td> cells, skipping. Content: {row_content_for_log}...")
                continue
            
            # Assuming the row is a data row based on the new heuristic (position in table)
            if len(td_cells) > 1: # Need at least two cells for name/status
                status_text_cell_content = td_cells[1].get_text(strip=True)
                status_class_list = td_cells[1].get('class', [])
                if isinstance(status_class_list, str): status_class_list = status_class_list.split()

                final_status_text = status_text_cell_content.upper() 
                if status_class_list:
                    for cls in status_class_list:
                        if cls.startswith('status-'):
                            status_from_class = cls.split('-',1)[1].upper()
                            # Be more specific about valid status prefixes from class
                            if status_from_class in ['P', 'F', 'K', 'U', 'PASSED', 'FAILED', 'KILLED', 'UNKNOWN']: 
                                final_status_text = status_from_class
                                break
                
                stats['total'] += 1
                if final_status_text == 'P' or final_status_text == 'PASSED':
                    stats['passed'] += 1
                elif final_status_text == 'F' or final_status_text == 'FAILED':
                    stats['failed'] += 1
                elif final_status_text == 'K' or final_status_text == 'KILLED': # Assuming K or KILLED for killed status
                    stats['killed'] += 1
                else: # Includes UNKNOWN, U, or any other text not matched above
                    stats['other'] += 1 
                    if job_id_for_logging:
                        add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Case '{td_cells[0].get_text(strip=True)[:50]}...' status '{final_status_text}' (from text: '{status_text_cell_content}', class: '{status_class_list}') categorized as 'Other/Unknown'.")
            else: 
                if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Debug_HTML_Stats: Valid data row (checkbox found) but <2 cells: {row.get_text(strip=True, separator='|')[:70]}...")

        if stats['total'] > 0:
            stats['pass_rate'] = (stats['passed'] / stats['total']) * 100
        
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Successfully calculated total stats from {html_file_path}")
        return stats
    except Exception as e:
        error_msg = f"Error calculating total HTML stats from '{html_file_path}': {e}"
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, error_msg)
        log_func(error_msg, "error")
        import traceback
        traceback.print_exc() # For server console debugging
        return None

def long_running_rerun_task(job_id, options, current_app_logger, actual_flask_app_instance): # Added actual_flask_app_instance
    # Raw print to see if the thread function is entered at all
    print(f"[THREAD_DEBUG] long_running_rerun_task entered for job_id: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    rerun_log_path = None
    rerun_log_file_handle = None
    html_report_actual_path = options.get('html_report_actual_path')

    if html_report_actual_path:
        try:
            # Ensure the directory for the HTML report exists before trying to create rerun.log in it
            html_report_dir = os.path.dirname(html_report_actual_path)
            if os.path.isdir(html_report_dir): # Check if it's a directory
                rerun_log_path = os.path.join(html_report_dir, "rerun.log")
            else:
                # This case should be rare if html_report_actual_path is a valid file path
                print(f"Warning: Directory for HTML report '{html_report_dir}' does not exist. Cannot create rerun.log there.")
                # Optionally log to job status if add_output_line_to_job is available early
                # add_output_line_to_job(job_id, f"Warning: HTML report directory '{html_report_dir}' invalid. rerun.log disabled.")
        except Exception as e_path:
            print(f"Error determining rerun_log_path from '{html_report_actual_path}': {e_path}")


    # Ensure the entire function is wrapped in a try-except to catch early failures
    try:
        # from backend.app import app as main_flask_app # Removed import, will use passed instance
        print(f"[THREAD_DEBUG] job_id: {job_id} - Using passed actual_flask_app_instance: {actual_flask_app_instance}")

        # Open rerun.log if path is valid
        if rerun_log_path:
            try:
                rerun_log_file_handle = open(rerun_log_path, 'a', encoding='utf-8')
                rerun_log_file_handle.write(f"\n{'='*20} Rerun Job Log Started: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')} {'='*20}\n")
                rerun_log_file_handle.flush() # Ensure header is written immediately
            except Exception as e_fopen:
                print(f"CRITICAL: Failed to open rerun.log at '{rerun_log_path}': {e_fopen}")
                # add_output_line_to_job(job_id, f"Error: Failed to open rerun.log for writing: {e_fopen}") # Careful with logging before logger is set
                rerun_log_file_handle = None # Ensure it's None if open failed

        with actual_flask_app_instance.app_context(): # Use passed instance
            print(f"[THREAD_DEBUG] job_id: {job_id} - Successfully entered app_context.")

            logger_to_use_start = current_app_logger if current_app_logger else actual_flask_app_instance.logger # Use logger from passed instance

            if not logger_to_use_start: # Defensive check
                print(f"[THREAD_DEBUG] job_id: {job_id} - ERROR: logger_to_use_start is None or not valid. Using fallback print logger for initial log.")
                class PrintLoggerThreadFallback:
                    def info(self, msg): print(f"INFO (thread_fallback_logger): {msg}")
                    def error(self, msg, exc_info=False):
                        print(f"ERROR (thread_fallback_logger): {msg}")
                        if exc_info: import traceback; traceback.print_exc()
                logger_to_use_start = PrintLoggerThreadFallback()

            print(f"[THREAD_DEBUG] job_id: {job_id} - Logger configured. Attempting first log message via logger.")
            logger_to_use_start.info(f"--- Starting long_running_rerun_task for job_id: {job_id} (WITH APP CONTEXT) ---")

            if options:
                logger_to_use_start.info(f"Job {job_id}: Relevant options for path derivation - url_repo_id: {options.get('url_repo_id')}, db_project_base_path: {options.get('db_project_base_path')}, branchPath: {options.get('branchPath')}")

            # This is the original start of the main try block, now nested
            # try: # OUTER TRY (original name, now effectively inner to the new top-level try)
            options["job_id_for_logging"] = job_id # Ensure this is set for add_output_line_to_job
            print(f"[THREAD_DEBUG] job_id: {job_id} - Set job_id_for_logging in options.")

            project_root_for_icenv = None
            branch_path_from_options = options.get('branchPath')
            
            logger_to_use_start.info(f"Job {job_id}: Attempting to determine project root solely from client-provided 'branchPath': '{branch_path_from_options}'.")

            if branch_path_from_options and isinstance(branch_path_from_options, str) and '/work/' in branch_path_from_options:
                project_root_for_icenv = get_project_root_from_branch_path(branch_path_from_options, job_id)
                if project_root_for_icenv:
                    add_output_line_to_job(job_id, f"Successfully derived project root from client 'branchPath' ('{branch_path_from_options}'): {project_root_for_icenv}")
                    logger_to_use_start.info(f"Job {job_id}: Successfully derived project_root_for_icenv from client 'branchPath': {project_root_for_icenv}")
                else:
                    # This case (get_project_root_from_branch_path returns None despite valid-looking input) should be rare.
                    project_root_for_icenv = None # Explicitly set to None
                    error_msg = f"Error: Failed to derive project root from 'branchPath' ('{branch_path_from_options}') even though it appeared valid. Ensure the path structure is correct."
                    add_output_line_to_job(job_id, error_msg)
                    logger_to_use_start.error(f"Job {job_id}: {error_msg}")
            else:
                error_msg = ""
                if not branch_path_from_options or not isinstance(branch_path_from_options, str):
                    error_msg = "Error: 'branchPath' was not provided by the client or is not a string."
                elif '/work/' not in branch_path_from_options:
                    error_msg = f"Error: Provided 'branchPath' ('{branch_path_from_options}') is invalid as it does not contain '/work/'."
                
                add_output_line_to_job(job_id, error_msg)
                logger_to_use_start.error(f"Job {job_id}: {error_msg}")
                # project_root_for_icenv remains None

            # If project_root_for_icenv could not be determined, fail the job.
            if not project_root_for_icenv:
                error_msg_proj_root = "Critical: Failed to determine project root. A valid 'branchPath' (string containing '/work/') must be provided by the client in the request options."
                update_job_status(job_id, "failed", error_msg_proj_root)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_msg_proj_root}\n")
                # Specific error logged above, no need for redundant logger_to_use_start.error here.
                return
            
            # Log the final determined project root (this line was already present and is correct)
            log_msg_proj_root_success = f"Job {job_id}: Successfully determined final project_root_for_icenv: {project_root_for_icenv}"
            logger_to_use_start.info(log_msg_proj_root_success)
            if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {log_msg_proj_root_success}\n")


            num_selected_cases = len(options.get('selectedCases', []))
            JOB_STATUS[job_id]['progress_summary'] = {"total_selected": num_selected_cases, "processed_count": 0, "passed_count": 0, "failed_count": 0}
            update_job_status(job_id, "preparing_hjson", "Preparing HJSON files...")
            add_output_line_to_job(job_id, "Rerun task started. Preparing HJSON files...")
            # ... (temp_rerun_dir creation, IP name derivation, HJSON file preparation loop as before) ...
            temp_rerun_dir_name = f"temp_rerun_{job_id}_{str(uuid.uuid4())[:8]}"
            temp_rerun_dir = os.path.join(script_dir, temp_rerun_dir_name)
            try:
                os.makedirs(temp_rerun_dir, exist_ok=True)
            except Exception as e:
                err_msg_temp_dir = f"Failed to create temp directory: {e}"
                update_job_status(job_id, "failed", err_msg_temp_dir)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_temp_dir}\n")
                return

            ip_derivation_path = options.get('branchPath')
            if not ip_derivation_path:
                err_msg_ip_path = "Branch path (for IP derivation) not provided."
                update_job_status(job_id, "failed", err_msg_ip_path)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_ip_path}\n")
                return
            derived_ip_name = None
            try:
                ip_folder_name = os.path.basename(ip_derivation_path)
                derived_ip_name = ip_folder_name.split('-', 1)[0]
                if not derived_ip_name: raise ValueError("Derived IP name is empty.")
            except Exception as e:
                err_msg_ip_derive = f"Failed to derive IP name from branch path: {ip_derivation_path}. Error: {e}"
                update_job_status(job_id, "failed", err_msg_ip_derive)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_ip_derive}\n")
                return

            ip_names_to_process = {derived_ip_name}
            generated_hjson_paths_map = {}
            all_hjson_prepared_successfully = True
            for ip_name in ip_names_to_process:
                hjson_path = prepare_rerun_hjson_files(project_root_for_icenv, options, temp_rerun_dir, ip_name) # This function logs to job_id internally
                if hjson_path: generated_hjson_paths_map[ip_name] = hjson_path
                else: all_hjson_prepared_successfully = False; break # prepare_rerun_hjson_files should log its own errors to job_id
            
            if not all_hjson_prepared_successfully:
                # Error already logged by prepare_rerun_hjson_files to job_id, and potentially to rerun.log if that function is modified too.
                # For now, just update status and log here.
                err_msg_hjson_prep = "HJSON prep failed for one or more IPs."
                update_job_status(job_id, "failed", err_msg_hjson_prep)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_hjson_prep} (check job output for details from prepare_rerun_hjson_files)\n")
                return
            if not generated_hjson_paths_map:
                err_msg_no_hjson = "No HJSON files generated (or no IPs to process)."
                update_job_status(job_id, "failed", err_msg_no_hjson)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_no_hjson}\n")
                return
            
            status_msg_hjson_done = "HJSON files prepared. Starting Git Pull..." # Changed next step
            update_job_status(job_id, "hjson_prepared", status_msg_hjson_done)
            if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {status_msg_hjson_done}\n")
            # ... (msim command parts assembly as before) ...
            prepared_hjson_actual_path = list(generated_hjson_paths_map.values())[0]
            msim_command_parts = ["msim", "rerun", "-t", "rerun"]
            if not options.get('rebuildCases', False): msim_command_parts.append("-so")
            if options.get('includeWaveform'): msim_command_parts.append("-w")
            # ... (other msim options) ...
            sim_time_hours_str = options.get('simTimeHours', "0")
            try:
                sim_time_hours = int(sim_time_hours_str)
                if sim_time_hours > 0: msim_command_parts.extend(["-rto", str(sim_time_hours * 60)])
            except ValueError: pass # Ignore invalid simTimeHours

            user_specified_dir_option = options.get('dirOption', '').strip()
            # ... (final_msim_dir_option logic) ...
            full_branch_path_for_msim_dir_derivation = options.get('branchPath', '')
            final_msim_dir_option = None
            if user_specified_dir_option: final_msim_dir_option = user_specified_dir_option
            else:
                if "work/" in full_branch_path_for_msim_dir_derivation:
                    path_after_work = full_branch_path_for_msim_dir_derivation.split("work/", 1)[1]
                    derived_dir_for_msim = os.path.normpath(path_after_work).split(os.sep)[0]
                    if derived_dir_for_msim and derived_dir_for_msim != '.' and derived_dir_for_msim != os.path.basename(path_after_work):
                        final_msim_dir_option = derived_dir_for_msim
            if final_msim_dir_option: msim_command_parts.extend(["-dir", final_msim_dir_option])
            # ... (elab, vlog, ro opts) ...
            elab_opts_value = options.get('elabOpts', '').strip()
            if elab_opts_value: msim_command_parts.extend(["-elab", elab_opts_value])
            vlogan_opts_value = options.get('vloganOpts', '').strip()
            if vlogan_opts_value: msim_command_parts.extend(["-vlog", vlogan_opts_value])
            run_opts_value = options.get('runOpts', '').strip()
            if run_opts_value: msim_command_parts.extend(["-ro", run_opts_value])

            msim_executable_and_args = " ".join(msim_command_parts)
            icenv_script_path = "source /remote/public/scripts/icenv.csh"
            module_load_command = "module load msim/v3p0"
            # Corrected: git_pull_dir is project_root_for_icenv
            git_pull_dir = project_root_for_icenv 
            
            # Sequence: icenv setup, module load, cd to project_root_for_icenv (which is git_pull_dir), git pull, then msim
            # Note: The Popen cwd is already project_root_for_icenv.
            # So, the 'cd {git_pull_dir}' is only strictly necessary if project_root_for_icenv could somehow be different
            # from the Popen cwd, or for explicit clarity. Given Popen's cwd IS project_root_for_icenv,
            # The Popen cwd is already project_root_for_icenv (which is git_pull_dir).
            # So, an explicit 'cd {git_pull_dir}' inside the shell command is redundant.
            shell_exec_command = (
                f"source ~/.cshrc && "
                f"{icenv_script_path} && "
                f"{module_load_command} && "
                # f"cd {git_pull_dir} && "  # Removed redundant cd
                f"git pull && "
                f"{msim_executable_and_args}"
            )
            
            # --- Stage 1: Git Pull ---
            update_job_status(job_id, "git_pulling", f"Pulling latest changes in {git_pull_dir}...")
            git_pull_shell_command = (
                f"source ~/.cshrc && "
                f"{icenv_script_path} && "
                f"{module_load_command} && "
                f"git pull"
            )
            add_output_line_to_job(job_id, f"Executing Git pull (CWD: {git_pull_dir}): {git_pull_shell_command}")
            if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: Executing Git pull (CWD: {git_pull_dir}): {git_pull_shell_command}\n")
            logger_to_use_start.info(f"Job {job_id}: Executing Git pull command: {git_pull_shell_command} in CWD: {git_pull_dir}")

            git_pull_success = False
            try:
                process_git_pull = subprocess.Popen(git_pull_shell_command,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, # Merge stderr to stdout
                                                 text=True, bufsize=1, universal_newlines=True,
                                                 shell=True, executable='tcsh', cwd=git_pull_dir)
                if process_git_pull.stdout:
                    for line in iter(process_git_pull.stdout.readline, ''):
                        stripped_line = line.strip()
                        add_output_line_to_job(job_id, f"[GIT PULL] {stripped_line}")
                        if rerun_log_file_handle: rerun_log_file_handle.write(f"[GIT PULL] {stripped_line}\n")
                    process_git_pull.stdout.close()

                git_pull_return_code = process_git_pull.wait()
                # stderr is merged, so no separate stderr reading here.
                
                if git_pull_return_code == 0:
                    msg_git_ok = "Git pull successful."
                    add_output_line_to_job(job_id, msg_git_ok)
                    if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {msg_git_ok}\n")
                    logger_to_use_start.info(f"Job {job_id}: Git pull successful.")
                    git_pull_success = True
                else:
                    error_message_git_pull = f"Git pull failed with return code {git_pull_return_code}."
                    add_output_line_to_job(job_id, error_message_git_pull)
                    if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_message_git_pull}\n") # Log combined output already handled
                    logger_to_use_start.error(f"Job {job_id}: {error_message_git_pull}")
                    update_job_status(job_id, "failed", error_message_git_pull) # stderr already part of stdout
                    return # Exit task

            except FileNotFoundError:
                error_message_fnf_git = "Shell (tcsh) or git command not found during git pull. Ensure tcsh and git are accessible in PATH."
                add_output_line_to_job(job_id, f"Error: {error_message_fnf_git}")
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_message_fnf_git}\n")
                logger_to_use_start.error(f"Job {job_id}: {error_message_fnf_git}")
                update_job_status(job_id, "failed", error_message_fnf_git)
                return
            except Exception as e_git_pull:
                error_message_exc_git = f"An error occurred during git pull: {e_git_pull}"
                add_output_line_to_job(job_id, f"Error: {error_message_exc_git}")
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_message_exc_git}\n")
                logger_to_use_start.error(f"Job {job_id}: {error_message_exc_git}", exc_info=True)
                update_job_status(job_id, "failed", error_message_exc_git)
                return

            if not git_pull_success: # Safeguard, should have been caught by returns above
                logger_to_use_start.error(f"Job {job_id}: Git pull was marked as unsuccessful but did not return early. Forcing failure.")
                update_job_status(job_id, "failed", "Git pull reported as failed (safeguard), aborting task.")
                return

            # --- Stage 2: Prepare HJSON files (uses updated files from git pull) ---
            status_msg_hjson_prep_post_git = "Preparing HJSON files (post git pull)..."
            update_job_status(job_id, "preparing_hjson", status_msg_hjson_prep_post_git)
            if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {status_msg_hjson_prep_post_git}\n")
            logger_to_use_start.info(f"Job {job_id}: Starting HJSON preparation after successful git pull.")
            
            generated_hjson_paths_map_post_git = {} # Use a new map name to avoid confusion
            all_hjson_prepared_successfully_post_git = True
            for ip_name_hjson_prep_pg in ip_names_to_process: # ip_names_to_process is defined before git pull
                hjson_path_pg = prepare_rerun_hjson_files(project_root_for_icenv, options, temp_rerun_dir, ip_name_hjson_prep_pg)
                if hjson_path_pg:
                    generated_hjson_paths_map_post_git[ip_name_hjson_prep_pg] = hjson_path_pg
                else:
                    all_hjson_prepared_successfully_post_git = False
                    # prepare_rerun_hjson_files logs its own errors to job_id. We log to rerun.log here.
                    err_msg_hjson_prep_pg = f"HJSON preparation failed for IP: {ip_name_hjson_prep_pg} (post git pull)."
                    if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_hjson_prep_pg}\n")
                    logger_to_use_start.error(f"Job {job_id}: {err_msg_hjson_prep_pg}")
                    break 
            
            if not all_hjson_prepared_successfully_post_git:
                err_msg_hjson_fail_pg = "HJSON preparation failed for one or more IPs (post git pull)."
                update_job_status(job_id, "failed", err_msg_hjson_fail_pg)
                # rerun.log already has specific IP failure if any.
                return
            if not generated_hjson_paths_map_post_git:
                err_msg_no_hjson_pg = "No HJSON files were generated after git pull (or no IPs to process)."
                update_job_status(job_id, "failed", err_msg_no_hjson_pg)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {err_msg_no_hjson_pg}\n")
                return

            # --- Stage 3: MSIM Execution ---
            status_msg_msim_start = "HJSON files prepared. Starting MSIM..."
            update_job_status(job_id, "hjson_prepared", status_msg_msim_start) # Status indicates HJSON is done, msim is next
            if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {status_msg_msim_start}\n")
            logger_to_use_start.info(f"Job {job_id}: HJSON files prepared. Assembling MSIM command.")
            
            msim_shell_command = (
                f"source ~/.cshrc && "
                f"{icenv_script_path} && "
                f"{module_load_command} && "
                f"echo 'DIAG_TRACE: Checking PRJ_ICDIR before msim execution.' && "
                f"echo 'DIAG_PRJ_ICDIR_VALUE: '$PRJ_ICDIR && "
                f"{msim_executable_and_args}" # msim_executable_and_args is defined before git pull stage
            )
            update_job_status(job_id, "running_msim", f"Executing MSIM command in {git_pull_dir}...", 
                              command=f"{msim_executable_and_args} (executed in {git_pull_dir} after icenv setup with PRJ_ICDIR diagnostic)")
            
            msim_exec_log_msg1 = f"Executing MSIM (CWD: {git_pull_dir}): {msim_shell_command}"
            msim_exec_log_msg2 = "This may take some time..."
            add_output_line_to_job(job_id, msim_exec_log_msg1)
            add_output_line_to_job(job_id, msim_exec_log_msg2)
            if rerun_log_file_handle: 
                rerun_log_file_handle.write(f"INFO: {msim_exec_log_msg1}\n")
                rerun_log_file_handle.write(f"INFO: {msim_exec_log_msg2}\n")
            logger_to_use_start.info(f"Job {job_id}: Executing MSIM command: {msim_shell_command} in CWD: {git_pull_dir}")

            process_return_code = None 
            msim_stdout_start_index = len(JOB_STATUS[job_id].get("output_lines", [])) 

            try: 
                add_output_line_to_job(job_id, "Using inherited environment for MSIM subprocess.") # This line will also go to rerun.log if handled by a wrapper
                if rerun_log_file_handle: rerun_log_file_handle.write("INFO: Using inherited environment for MSIM subprocess.\n")
                
                msim_attempt_msg = f"Attempting to execute MSIM shell command with tcsh. Ensure 'tcsh' is in the inherited PATH."
                add_output_line_to_job(job_id, msim_attempt_msg)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {msim_attempt_msg}\n")

                process_msim = subprocess.Popen(msim_shell_command,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, # Merge stderr to stdout
                                             text=True, bufsize=1, universal_newlines=True,
                                             shell=True, executable='tcsh', cwd=git_pull_dir) 

                if process_msim.stdout:
                    for line in iter(process_msim.stdout.readline, ''):
                        stripped_line = line.strip() 
                        add_output_line_to_job(job_id, stripped_line) 
                        if rerun_log_file_handle: rerun_log_file_handle.write(stripped_line + "\n")
                    process_msim.stdout.close()

                process_return_code = process_msim.wait()
                # stderr is merged.

                final_status_key_msim = "completed" if process_return_code == 0 else "failed"
                final_status_message_msim = f"MSIM run {'completed successfully' if process_return_code == 0 else f'failed with return code {process_return_code}'}."
                update_job_status(job_id, final_status_key_msim, final_status_message_msim, returncode=process_return_code)
                add_output_line_to_job(job_id, final_status_message_msim)
                if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {final_status_message_msim}\n")
                logger_to_use_start.info(f"Job {job_id}: {final_status_message_msim}")

            except FileNotFoundError:
                process_return_code = -1 
                error_message_fnf_msim = "Shell (tcsh) or msim command not found. Ensure tcsh and msim are accessible."
                add_output_line_to_job(job_id, f"Error: {error_message_fnf_msim}")
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_message_fnf_msim}\n")
                logger_to_use_start.error(f"Job {job_id}: {error_message_fnf_msim}")
                update_job_status(job_id, "failed", error_message_fnf_msim)
                return 
            except Exception as e_msim_popen:
                process_return_code = -1 
                error_message_exc_msim = f"An error occurred during MSIM execution: {e_msim_popen}"
                add_output_line_to_job(job_id, f"Error: {error_message_exc_msim}")
                if rerun_log_file_handle: rerun_log_file_handle.write(f"ERROR: {error_message_exc_msim}\n")
                logger_to_use_start.error(f"Job {job_id}: {error_message_exc_msim}", exc_info=True)
                update_job_status(job_id, "failed", error_message_exc_msim)
                return # Exit task

            # --- Stage 4: Post MSIM processing ---
            # Isolate MSIM-specific stdout for parsing
            msim_specific_output_lines = JOB_STATUS[job_id].get("output_lines", [])[msim_stdout_start_index:]
            full_msim_stdout_for_parsing = "\n".join(msim_specific_output_lines)
            
            proj_root_dir_for_logs = project_root_for_icenv # This is git_pull_dir
            actual_sim_root_for_parsing = None; base_log_path_for_html = None; log_path_error = False

            if not proj_root_dir_for_logs: # Should not happen if we reached here
                add_output_line_to_job(job_id, "CRITICAL Error: Project root not available for log parsing (post-msim).")
                log_path_error = True
            else:
                vcs_context_from_client_for_log_parsing = options.get('vcsContext', '')
                vcs_context_basename = os.path.basename(vcs_context_from_client_for_log_parsing) if vcs_context_from_client_for_log_parsing else ""
                if not vcs_context_basename: # Fallback to deriving from branchPath if vcsContext is empty
                    current_full_branch_path_for_log_parsing = options.get('branchPath', '') # This is the original branchPath from client
                    if current_full_branch_path_for_log_parsing:
                        vcs_context_basename = os.path.basename(current_full_branch_path_for_log_parsing)
                
                if not vcs_context_basename: # If still no vcs_context_basename
                    add_output_line_to_job(job_id, "Warning: Could not determine vcs_context_basename for log path construction.")
                    log_path_error = True
                else:
                    # final_msim_dir_option is determined earlier in the function
                    if final_msim_dir_option:
                        actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename, "sim")
                        base_log_path_for_html = os.path.join("work", final_msim_dir_option, vcs_context_basename)
                    else: # Fallback if -dir was not used or derived
                        current_full_branch_path_for_log_parsing = options.get('branchPath', '')
                        extracted_branch_suffix = ""
                        if "work/" in current_full_branch_path_for_log_parsing:
                            extracted_branch_suffix = current_full_branch_path_for_log_parsing.split("work/", 1)[1]
                        
                        if not extracted_branch_suffix:
                            add_output_line_to_job(job_id, "Warning: Could not extract branch suffix from branchPath for log path construction.")
                            log_path_error = True
                        else:
                            actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", extracted_branch_suffix, "sim")
                            base_log_path_for_html = os.path.join("work", extracted_branch_suffix)

            if not log_path_error:
                add_output_line_to_job(job_id, f"  Final calculated absolute sim root for parsing (post-msim): {actual_sim_root_for_parsing}")
                add_output_line_to_job(job_id, f"  Final calculated base relative path for HTML logs (post-msim): {base_log_path_for_html}")

            # Use full_msim_stdout_for_parsing which contains only MSIM output
            if log_path_error or not actual_sim_root_for_parsing or not os.path.isdir(actual_sim_root_for_parsing):
                 add_output_line_to_job(job_id, "Warning: Log path error or invalid sim root. Parsing MSIM stdout without specific log file checks.")
                 detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout_for_parsing, options.get('selectedCases', []), None, None, job_id)
            else:
                detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout_for_parsing, options.get('selectedCases', []), actual_sim_root_for_parsing, base_log_path_for_html, job_id)
            
            JOB_STATUS[job_id]['detailed_test_results'] = detailed_results
            add_output_line_to_job(job_id, f"Final detailed test results (post-msim): {detailed_results}")

            # Update HTML report on disk
            if detailed_results and (JOB_STATUS[job_id]['status'] == "completed" or JOB_STATUS[job_id]['status'] == "failed"):
                # html_report_actual_path is already defined at the top of the function
                if html_report_actual_path: # Use the path determined at the start
                    msg_html_update = f"Attempting to update HTML report on disk at: {html_report_actual_path}"
                    add_output_line_to_job(job_id, msg_html_update)
                    if rerun_log_file_handle: rerun_log_file_handle.write(f"INFO: {msg_html_update}\n")
                    update_html_report_on_disk(html_report_actual_path, detailed_results, job_id, project_root_for_icenv, None, derived_ip_name, logger_to_use_start)
                else:
                    # This block for fallback might be less relevant if html_report_actual_path is robustly obtained
                    msg_html_warn_fallback = "Warning: 'html_report_actual_path' was not available. Cannot update HTML report on disk."
                    add_output_line_to_job(job_id, msg_html_warn_fallback)
                    if rerun_log_file_handle: rerun_log_file_handle.write(f"WARN: {msg_html_warn_fallback}\n")
                    logger_to_use_start.warning(f"Job {job_id}: {msg_html_warn_fallback}")
            
            # --- Requirement 2: Log total HTML stats to rerun.log ---
            if rerun_log_file_handle and html_report_actual_path and os.path.exists(html_report_actual_path):
                rerun_log_file_handle.write("\nINFO: Calculating total HTML report statistics...\n")
                total_stats = calculate_total_stats_from_html(html_report_actual_path, job_id, logger_to_use_start)
                if total_stats:
                    rerun_log_file_handle.write("\n--- Total HTML Report Status Summary (all cases) ---\n")
                    rerun_log_file_handle.write(f"Total Cases In HTML: {total_stats['total']}\n")
                    rerun_log_file_handle.write(f"Passed: {total_stats['passed']} ({total_stats['pass_rate']:.2f}%)\n")
                    rerun_log_file_handle.write(f"Failed: {total_stats['failed']}\n")
                    rerun_log_file_handle.write(f"Killed: {total_stats['killed']}\n")
                    rerun_log_file_handle.write(f"Other/Unknown: {total_stats['other']}\n")
                    rerun_log_file_handle.write("---------------------------------------------------\n")
                    rerun_log_file_handle.flush() # Ensure summary is written
                else:
                    rerun_log_file_handle.write("ERROR: Failed to calculate total HTML report statistics.\n")

            if not log_path_error and proj_root_dir_for_logs and final_msim_dir_option and vcs_context_basename:
                rerun_output_base_abs = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename)
                find_primary_log_for_rerun(rerun_output_base_abs) 
            elif not log_path_error and proj_root_dir_for_logs and base_log_path_for_html and not final_msim_dir_option :
                rerun_output_base_abs_fallback = os.path.join(proj_root_dir_for_logs, base_log_path_for_html)
                find_primary_log_for_rerun(rerun_output_base_abs_fallback)

    except Exception as e_very_outer:
        error_message_top = f"CRITICAL UNCAUGHT EXCEPTION IN TASK {job_id}: {str(e_very_outer)}"
        print(f"[THREAD_DEBUG] {error_message_top}") 
        import traceback
        traceback.print_exc() 

        try:
            update_job_status(job_id, "failed", f"Critical uncaught error: {str(e_very_outer)}")
            if "job_id_for_logging" not in options: options["job_id_for_logging"] = job_id 
            add_output_line_to_job(job_id, error_message_top)
            add_output_line_to_job(job_id, traceback.format_exc())
            if rerun_log_file_handle:
                rerun_log_file_handle.write(f"CRITICAL_ERROR: {error_message_top}\n")
                rerun_log_file_handle.write(traceback.format_exc() + "\n")
        except Exception as e_status_update:
            print(f"[THREAD_DEBUG] job_id: {job_id} - Failed to update job status after top-level exception: {str(e_status_update)}")

        try:
            with actual_flask_app_instance.app_context(): 
                 logger_final_error = current_app_logger if current_app_logger else actual_flask_app_instance.logger
                 if logger_final_error:
                     logger_final_error.error(f"CRITICAL UNCAUGHT EXCEPTION IN TASK {job_id} (WITH APP CONTEXT): {str(e_very_outer)}", exc_info=True)
        except Exception as e_log_final:
            print(f"[THREAD_DEBUG] job_id: {job_id} - Failed to log final error using app logger: {str(e_log_final)}")
    finally:
        print(f"[THREAD_DEBUG] long_running_rerun_task finished or exited for job_id: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # --- Prepare Rerun Job Summary for HTML Terminal ---
        final_job_status_info = JOB_STATUS.get(job_id, {})
        overall_job_status_str = final_job_status_info.get('status', 'unknown').upper()
        
        # Calculate stats for *rerun cases*
        rerun_stats = {'total': 0, 'passed': 0, 'failed': 0, 'killed': 0, 'other': 0}
        detailed_results_for_summary = final_job_status_info.get('detailed_test_results', [])
        if detailed_results_for_summary:
            for res in detailed_results_for_summary:
                rerun_stats['total'] += 1
                status = res.get('status', 'UNKNOWN').upper()
                if status == 'PASSED' or status == 'P':
                    rerun_stats['passed'] += 1
                elif status == 'FAILED' or status == 'F':
                    rerun_stats['failed'] += 1
                # Assuming 'KILLED' or 'K' might appear in detailed_results if parsing supports it.
                # Currently, parse_msim_output_for_test_statuses mainly yields PASSED/FAILED/UNKNOWN.
                elif status == 'KILLED' or status == 'K': 
                    rerun_stats['killed'] += 1
                else:
                    rerun_stats['other'] += 1
        
        # Close log file (still goes to server console for this specific error)
        log_closed_successfully = False
        if rerun_log_file_handle:
            try:
                rerun_log_file_handle.write(f"{'='*20} Rerun Job Log Ended: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')} {'='*20}\n\n")
                rerun_log_file_handle.close()
                log_closed_successfully = True
            except Exception as e_fclose:
                # This print goes to server console, not HTML terminal
                print(f"Error closing rerun.log for job {job_id}: {e_fclose}")

        # Send summary to HTML terminal using add_output_line_to_job
        summary_banner_char = "#"
        summary_width = 60
        add_output_line_to_job(job_id, "\n" + summary_banner_char * summary_width)
        add_output_line_to_job(job_id, summary_banner_char + "R E R U N   J O B   S U M M A R Y".center(summary_width - 2) + summary_banner_char)
        add_output_line_to_job(job_id, summary_banner_char * summary_width)
        add_output_line_to_job(job_id, f"Job ID        : {job_id}")
        add_output_line_to_job(job_id, f"Overall Status: {overall_job_status_str}")
        
        add_output_line_to_job(job_id, "--- Rerun Cases Statistics ---")
        add_output_line_to_job(job_id, f"Total Rerun   : {rerun_stats['total']}")
        add_output_line_to_job(job_id, f"Passed        : {rerun_stats['passed']}")
        add_output_line_to_job(job_id, f"Failed        : {rerun_stats['failed']}")
        add_output_line_to_job(job_id, f"Killed        : {rerun_stats['killed']}") # Will be 0 if not explicitly set
        add_output_line_to_job(job_id, f"Other/Unknown : {rerun_stats['other']}")

        rerun_log_path_message = "Not generated (HTML report path likely missing or invalid)."
        if rerun_log_path:
            if log_closed_successfully:
                rerun_log_path_message = f"{rerun_log_path} (Closed)"
            elif os.path.exists(rerun_log_path):
                rerun_log_path_message = f"{rerun_log_path} (Exists, closure status uncertain for this run)"
            else:
                rerun_log_path_message = f"{rerun_log_path} (Intended, check creation/write errors)"
        add_output_line_to_job(job_id, f"Rerun Log File: {rerun_log_path_message}")
        add_output_line_to_job(job_id, summary_banner_char * summary_width + "\n")

        # Original server console logging for task exit
        try:
            with actual_flask_app_instance.app_context(): 
                 logger_final_exit = current_app_logger if current_app_logger else actual_flask_app_instance.logger
                 if logger_final_exit:
                    logger_final_exit.info(f"--- Task processing truly finished for job_id: {job_id} (WITH APP CONTEXT) ---")
        except Exception as e_log_exit:
            print(f"[THREAD_DEBUG] job_id: {job_id} - Failed to log task exit using app logger: {str(e_log_exit)}")


def update_html_report_on_disk(original_html_file_path, detailed_results, job_id_for_logging, project_root, original_dir, ip_name_for_report_path, logger_for_internal_errors):
    # ... (function content as before) ...
    constructed_path = False
    if not original_html_file_path:
        if project_root and original_dir and ip_name_for_report_path:
            original_html_file_path = os.path.join(project_root, "work", original_dir, "reports", "dv", "sim_ctrl", "ts", ip_name_for_report_path, "latest", "live_report.html")
            add_output_line_to_job(job_id_for_logging, f"Constructed original HTML report path: {original_html_file_path}")
            constructed_path = True
        else: return False
    if not os.path.exists(original_html_file_path): return False
    try:
        with open(original_html_file_path, 'r', encoding='utf-8') as f: soup = BeautifulSoup(f, 'html.parser')
        table_body = soup.find('table', id='detailedStatusTable').find('tbody')
        if not table_body: return False
        rows_updated_count = 0
        job_name_parse_regex_for_row = re.compile(r"^(.*?)\s*\(\s*Seed\s*:\s*(\d+)\s*\)$")
        for result in detailed_results:
            # ... (HTML row update logic as before) ...
            result_id_from_server = result['id']
            found_row_for_result = False
            for row in table_body.find_all('tr'):
                cells = row.find_all('td')
                if not cells: continue
                current_row_id = None
                checkbox = row.find('input', class_='rerun-checkbox')
                if checkbox and checkbox.has_attr('data-casename') and checkbox.has_attr('data-seed'):
                    base_name = checkbox['data-casename'].strip(); seed = checkbox['data-seed'].strip()
                    current_row_id = f"{base_name}_seed{seed}"
                else:
                    job_name_cell_text = cells[0].get_text(strip=True) if len(cells) > 0 else None
                    if job_name_cell_text:
                        match = job_name_parse_regex_for_row.match(job_name_cell_text)
                        if match: base_name = match.group(1).strip(); seed = match.group(2).strip(); current_row_id = f"{base_name}_seed{seed}"
                if current_row_id == result_id_from_server:
                    if len(cells) >= 5:
                        # ... (update status_cell, pass_rate_cell, log_path_cell, error_hint_cell) ...
                        status_cell = cells[1]; res_status_upper = result['status'].upper()
                        if res_status_upper == 'PASSED': status_cell.string = 'P'; status_cell['class'] = ['status-P']
                        elif res_status_upper == 'FAILED': status_cell.string = 'F'; status_cell['class'] = ['status-F']
                        # ... other statuses ...
                        else: status_cell.string = res_status_upper[0] if res_status_upper else 'U'; status_cell['class'] = [f'status-{status_cell.string.upper()}']
                        cells[2].string = '100%' if res_status_upper == 'PASSED' else '0%'
                        code_tag = cells[3].find('code')
                        if code_tag: code_tag.string = result['new_log_path']
                        else: cells[3].string = result['new_log_path'] # Fallback
                        cells[4].string = result['error_hint'] or ''
                        rows_updated_count += 1; found_row_for_result = True; break
            if not found_row_for_result: add_output_line_to_job(job_id_for_logging, f"Warning: Could not find matching row in HTML for test: {result_id_from_server}")
        if rows_updated_count > 0:
            with open(original_html_file_path, 'w', encoding='utf-8') as f: f.write(soup.prettify())
            return True
        return False
    except ImportError: # ... (BeautifulSoup error handling) ...
        return False
    except Exception as e: # ... (generic error handling) ...
        return False


@bp.route('/rerun/<repo_id>', methods=['POST'])
def rerun_cases(repo_id):
    _logger_instance = getattr(bp, 'logger', None)
    if hasattr(request, 'getBluePrintAppLogger'): _logger_instance = request.getBluePrintAppLogger()
    elif current_app: _logger_instance = current_app.logger
    if not _logger_instance: # Fallback logger
        class PrintLogger:
            def info(self, msg): print(f"INFO: {msg}")
            def warning(self, msg): print(f"WARN: {msg}")
            def error(self, msg, exc_info=False): print(f"ERROR: {msg}")
        _logger_instance = PrintLogger()
    current_op_logger = _logger_instance

    job_id = None
    try:
        print(f"[DEBUG_PRINT] rerun_cases for repo_id: {repo_id} at {time.strftime('%Y-%m-%d %H:%M:%S')} - TRY block entered.")
        current_op_logger.info(f"--- /rerun endpoint hit for repo_id: {repo_id} ---")
        project_base_path_from_db = None
        if Repo and db:
            repo_obj = Repo.query.get(repo_id)
            if repo_obj:
                project_base_path_from_db = getattr(repo_obj, 'data_path', None)
                # Extract the actual HTML report path from the latest test record
                if repo_obj.test_records and isinstance(repo_obj.test_records, list) and len(repo_obj.test_records) > 0:
                    latest_record = repo_obj.test_records[0]
                    html_report_path_from_db = latest_record.get('rpt')
                    if html_report_path_from_db and os.path.exists(html_report_path_from_db):
                        # Add this path to the data to be passed to the thread
                        data = request.get_json() # Get data first
                        if not data: data = {} # Ensure data is a dict
                        data['html_report_actual_path'] = html_report_path_from_db
                        current_op_logger.info(f"Extracted actual HTML report path for update: {html_report_path_from_db}")
                    else:
                        # Path not found in DB or doesn't exist, log warning, proceed without it
                        # The fallback in long_running_rerun_task will be used.
                        data = request.get_json() # Still need to get data
                        if not data: data = {}
                        current_op_logger.warning(f"Could not get valid 'rpt' path from DB for repo {repo_id} for HTML update.")
                else: # No test records
                    data = request.get_json()
                    if not data: data = {}
                    current_op_logger.warning(f"No test records found for repo {repo_id} to determine HTML report path for update.")
            else: # Repo object not found
                data = request.get_json()
                if not data: data = {}
                current_op_logger.warning(f"Repo object not found for id {repo_id}.")
        else: # Repo or db not available
            data = request.get_json()
            if not data: data = {}
            current_op_logger.warning("Database (Repo/db) not available. Cannot fetch HTML report path from DB.")

        if not data or 'selectedCases' not in data: # data might have been initialized to {}
            return jsonify({"status": "error", "message": "Invalid request or missing selectedCases"}), 400
        
        data['url_repo_id'] = repo_id # Ensure repo_id is in data
        if project_base_path_from_db: # This might be None if repo_obj was not found
            data['db_project_base_path'] = project_base_path_from_db

        passed_app_instance = current_app._get_current_object() # Get the actual app instance
        print(f"[DEBUG_PRINT] rerun_cases for repo_id: {repo_id} - Passing app instance to thread: {passed_app_instance}")

        job_id = str(uuid.uuid4())
        JOB_STATUS[job_id] = {"status": "queued", "message": "Rerun job queued.", "output_lines": []}
        # Pass the actual app instance to the thread
        thread = threading.Thread(target=long_running_rerun_task, args=(job_id, data, current_op_logger, passed_app_instance))
        thread.start()
        return jsonify({"status": "queued", "message": "Rerun job initiated.", "job_id": job_id})
    except Exception as e:
        print(f"[DEBUG_PRINT] rerun_cases for repo_id: {repo_id} - EXCEPT block. Error: {str(e)}")
        import traceback; traceback.print_exc()
        current_op_logger.error(f"Exception in /rerun for repo_id {repo_id}: {e}", exc_info=True)
        error_ref_id = job_id if job_id else str(uuid.uuid4()) + "_error_early"
        if error_ref_id not in JOB_STATUS : JOB_STATUS[error_ref_id] = {"status": "failed", "message": f"Server error: {str(e)}"}
        else: update_job_status(error_ref_id, "failed", f"Server error: {str(e)}")
        return jsonify({"status": "error", "message": f"Internal server error. Ref: {error_ref_id}.", "job_id": error_ref_id }), 500

@bp.route('/rerun_status/<job_id>', methods=['GET'])
def get_rerun_status_route(job_id):
    return jsonify(get_job_status(job_id))

@bp.route('/<repo_id>')
def index(repo_id):
    if not Repo or not db: return "Database support is not configured.", 500
    repo = Repo.query.get_or_404(repo_id)
    html_rpt_abs_path = None  # Initialize path variable
    attempted_source_info = "database (repo.result['html_rpt'])" # Default source info for error message

    # Try to get the path from the latest test record
    if repo.test_records and isinstance(repo.test_records, list) and len(repo.test_records) > 0:
        # Assuming test_records is sorted with the latest first
        latest_record = repo.test_records[0]
        html_rpt_abs_path = latest_record.get('rpt')
        attempted_source_info = "latest test record ('rpt' field)"

        logger_instance = getattr(bp, 'logger', getattr(current_app, 'logger', None))
        if not html_rpt_abs_path:
            log_message_missing_path = f"Repo {repo_id}: Latest test record found, but 'rpt' path is missing or None in it."
            if logger_instance:
                 logger_instance.warning(log_message_missing_path)
            else:
                 print(f"WARNING: {log_message_missing_path}")
    else:
        logger_instance = getattr(bp, 'logger', getattr(current_app, 'logger', None))
        log_message_no_records = f"Repo {repo_id}: No test records found to derive HTML report path."
        if logger_instance:
            logger_instance.warning(log_message_no_records)
        else:
            print(f"WARNING: {log_message_no_records}")

    # Validate the path
    if not html_rpt_abs_path or not os.path.isabs(html_rpt_abs_path) or not os.path.exists(html_rpt_abs_path):
        error_msg = f"HTML report not found or invalid for repo {repo_id}. Attempted path from {attempted_source_info}: '{html_rpt_abs_path}'"
        logger_instance = getattr(bp, 'logger', getattr(current_app, 'logger', None))
        if logger_instance:
            logger_instance.error(error_msg)
        else:
            print(f"ERROR: {error_msg}")
        return error_msg, 404

    success_msg = f"Serving HTML report for repo {repo_id} from path (from {attempted_source_info}): {html_rpt_abs_path}"
    logger_instance = getattr(bp, 'logger', getattr(current_app, 'logger', None))
    if logger_instance:
        logger_instance.info(success_msg)
    else:
        print(f"INFO: {success_msg}")

    return send_from_directory(os.path.dirname(html_rpt_abs_path), os.path.basename(html_rpt_abs_path))

if __name__ == '__main__':
    app = Flask(__name__)
    @bp.before_request
    def before_request_func():
        if not hasattr(request, 'getBluePrintAppLogger'):
             request.getBluePrintAppLogger = lambda: app.logger
    app.register_blueprint(bp, url_prefix='/live_reporter')
    CORS(app)
    print("Server starting (standalone blueprint mode)...")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
