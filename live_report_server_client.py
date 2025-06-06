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
    from extensions import db, socketio  # Assuming socketio is defined in extensions.py
    from flask_socketio import join_room
except ImportError:
    Repo = None
    db = None
    socketio = None  # Ensure socketio is None if import fails
    join_room = None # Ensure join_room is None if import fails
    print("Warning: 'models' or 'extensions' (with db, socketio) module not found, or flask_socketio not available. Database and/or SocketIO features will be disabled.")

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

def update_job_status(job_id, status, message=None, command=None, returncode=None, stdout=None, stderr=None, emit_socketio_update=True): # Added emit_socketio_update flag
    if job_id not in JOB_STATUS: 
        JOB_STATUS[job_id] = {"output_lines": [], "status": "initializing", "message": "Job initializing."}
    
    JOB_STATUS[job_id]['status'] = status
    if message is not None: JOB_STATUS[job_id]['message'] = message
    if command is not None: JOB_STATUS[job_id]['command'] = command
    if returncode is not None: JOB_STATUS[job_id]['returncode'] = returncode
    if stdout is not None: JOB_STATUS[job_id]['stdout'] = stdout
    if stderr is not None: JOB_STATUS[job_id]['stderr'] = stderr

    if emit_socketio_update and socketio: # Check if socketio instance is available
        try:
            # Prepare payload for SocketIO
            socketio_payload = {
                "job_id": job_id,
                "status_key": status,
                "message": JOB_STATUS[job_id]['message'] # Send the current message for this status
            }
            if returncode is not None:
                socketio_payload['returncode'] = returncode
            
            # For final states, or if more detail is always desired, send more of JOB_STATUS
            # For example, if status is 'completed' or 'failed':
            if status in ["completed", "failed", "hjson_prepared", "running_msim", "preparing_hjson"]: # Add more relevant states
                 socketio_payload['details'] = JOB_STATUS[job_id].get('progress_summary') # Example: send progress
                 if 'detailed_test_results' in JOB_STATUS[job_id]:
                     socketio_payload['detailed_test_results'] = JOB_STATUS[job_id]['detailed_test_results']


            socketio.emit('rerun_status_update', socketio_payload, namespace='/rerun_jobs', room=job_id)
            
            # Logging the emission
            logger_instance = getattr(current_app, 'logger', None) or getattr(bp, 'logger', None)
            if logger_instance:
                logger_instance.info(f"SocketIO: Emitted 'rerun_status_update' for job {job_id} to room {job_id}. Status: {status}")
            else:
                print(f"INFO (SocketIO): Emitted 'rerun_status_update' for job {job_id} to room {job_id}. Status: {status}")

        except Exception as e_sock:
            logger_instance_err = getattr(current_app, 'logger', None) or getattr(bp, 'logger', None)
            if logger_instance_err:
                logger_instance_err.error(f"SocketIO: Error emitting 'rerun_status_update' for job {job_id}: {e_sock}", exc_info=True)
            else:
                print(f"ERROR (SocketIO): Error emitting 'rerun_status_update' for job {job_id}: {e_sock}")
                import traceback
                traceback.print_exc()

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

# SocketIO event handler for clients joining a job-specific room
if socketio and join_room:  # Only define handlers if socketio and join_room were successfully imported
    @socketio.on('join_rerun_room', namespace='/rerun_jobs')
    def handle_join_rerun_room(data):
        job_id = data.get('job_id')
        logger_instance = getattr(current_app, 'logger', None) or getattr(bp, 'logger', None)

        if job_id:
            join_room(job_id)  # Client joins a room named after the job_id
            if logger_instance:
                logger_instance.info(f"SocketIO: Client {request.sid} joined room '{job_id}' in '/rerun_jobs' namespace.")
            else:
                print(f"INFO (SocketIO): Client {request.sid} joined room '{job_id}' in '/rerun_jobs' namespace.")
            
            # Send the current full status to the client that just joined
            current_status_data = get_job_status(job_id)
            initial_payload = {
                "job_id": job_id,
                "status_key": current_status_data.get('status'),
                "message": current_status_data.get('message'),
                "output_lines": current_status_data.get('output_lines', []), 
                "details": current_status_data # Send the whole current state
            }
            # Emit specifically to the client who just joined using their session ID (request.sid)
            socketio.emit('rerun_status_update', initial_payload, namespace='/rerun_jobs', room=request.sid)
            if logger_instance:
                logger_instance.info(f"SocketIO: Sent initial status for job {job_id} to client {request.sid}.")
        else:
            if logger_instance:
                logger_instance.warning(f"SocketIO: 'join_rerun_room' request from {request.sid} in '/rerun_jobs' missing 'job_id'.")
            else:
                print(f"WARNING (SocketIO): 'join_rerun_room' request from {request.sid} in '/rerun_jobs' missing 'job_id'.")
else:
    # This message will print when the server starts if SocketIO is not configured
    print("WARNING: SocketIO or join_room not available. SocketIO 'join_rerun_room' handler will not be registered.")

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

def long_running_rerun_task(job_id, options, current_app_logger, actual_flask_app_instance): # Added actual_flask_app_instance
    # Raw print to see if the thread function is entered at all
    print(f"[THREAD_DEBUG] long_running_rerun_task entered for job_id: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Ensure the entire function is wrapped in a try-except to catch early failures
    try:
        # from backend.app import app as main_flask_app # Removed import, will use passed instance
        print(f"[THREAD_DEBUG] job_id: {job_id} - Using passed actual_flask_app_instance: {actual_flask_app_instance}")

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
            db_project_base_path_from_options = options.get('db_project_base_path')
            logger_to_use_start.info(f"Job {job_id}: Attempting to determine project root. Client branchPath: '{branch_path_from_options}', DB base_path: '{db_project_base_path_from_options}'.")

            # Priority 1: Derive from branchPath if it's valid and contains '/work/'
            if branch_path_from_options and '/work/' in branch_path_from_options:
                project_root_for_icenv = get_project_root_from_branch_path(branch_path_from_options, job_id)
                if project_root_for_icenv:
                    add_output_line_to_job(job_id, f"Using project root derived from client 'branchPath' ({branch_path_from_options}): {project_root_for_icenv}")
                    logger_to_use_start.info(f"Job {job_id}: Prioritized project_root_for_icenv from client branchPath: {project_root_for_icenv}")
                else: 
                    add_output_line_to_job(job_id, f"Warning: Failed to derive project root from client 'branchPath' ({branch_path_from_options}) even though '/work/' was present. Will check DB path.")
                    logger_to_use_start.warning(f"Job {job_id}: Failed to derive project_root_for_icenv from client branchPath ('{branch_path_from_options}') despite '/work/' presence. Checking DB path.")
            else:
                if branch_path_from_options:
                     add_output_line_to_job(job_id, f"Client 'branchPath' ({branch_path_from_options}) does not contain '/work/' or is invalid. Will check DB path.")
                     logger_to_use_start.info(f"Job {job_id}: Client 'branchPath' ('{branch_path_from_options}') unsuitable for derivation. Checking DB path.")
                else:
                     add_output_line_to_job(job_id, "Client 'branchPath' not provided. Will check DB path.")
                     logger_to_use_start.info(f"Job {job_id}: Client 'branchPath' not provided. Checking DB path.")


            # Priority 2: Use db_project_base_path if derivation from branchPath failed or branchPath was unsuitable
            if not project_root_for_icenv:
                if db_project_base_path_from_options:
                    project_root_for_icenv = db_project_base_path_from_options
                    add_output_line_to_job(job_id, f"Using project root from database ('db_project_base_path') as fallback: {project_root_for_icenv}")
                    logger_to_use_start.info(f"Job {job_id}: Using project_root_for_icenv from DB ('db_project_base_path') as fallback: {project_root_for_icenv}")
                else:
                    add_output_line_to_job(job_id, "Error: Client 'branchPath' was unsuitable for derivation, and 'db_project_base_path' was not available.")
                    logger_to_use_start.error(f"Job {job_id}: Client 'branchPath' unsuitable and 'db_project_base_path' (Repo.data_path) missing for project root determination.")
            
            if not project_root_for_icenv: # If still no root after all attempts
                update_job_status(job_id, "failed", "Critical: Failed to determine project root from any source (branchPath or DB). Cannot proceed.")
                logger_to_use_start.error(f"Job {job_id}: Failed to determine project_root_for_icenv from any source. Aborting task.")
                return
            
            # Log the final determined project root
            logger_to_use_start.info(f"Job {job_id}: Successfully determined final project_root_for_icenv: {project_root_for_icenv}")

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
                update_job_status(job_id, "failed", f"Failed to create temp directory: {e}"); return
            
            ip_derivation_path = options.get('branchPath') 
            if not ip_derivation_path: 
                update_job_status(job_id, "failed", "Branch path (for IP derivation) not provided."); return
            derived_ip_name = None
            try: 
                ip_folder_name = os.path.basename(ip_derivation_path) 
                derived_ip_name = ip_folder_name.split('-', 1)[0]
                if not derived_ip_name: raise ValueError("Derived IP name is empty.")
            except Exception as e:
                update_job_status(job_id, "failed", f"Failed to derive IP name from branch path: {ip_derivation_path}. Error: {e}"); return
            
            ip_names_to_process = {derived_ip_name}
            generated_hjson_paths_map = {}
            all_hjson_prepared_successfully = True
            for ip_name in ip_names_to_process:
                hjson_path = prepare_rerun_hjson_files(project_root_for_icenv, options, temp_rerun_dir, ip_name)
                if hjson_path: generated_hjson_paths_map[ip_name] = hjson_path
                else: all_hjson_prepared_successfully = False; break
            if not all_hjson_prepared_successfully: update_job_status(job_id, "failed", "HJSON prep failed."); return
            if not generated_hjson_paths_map: update_job_status(job_id, "failed", "No HJSON files generated."); return
            
            update_job_status(job_id, "hjson_prepared", "HJSON files prepared. Starting MSIM...")
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
            icenv_script_path = "/remote/public/scripts/icenv.csh"
            module_load_command = "module load msim/v3p0"
            shell_exec_command = f"source ~/.cshrc && {icenv_script_path} && {module_load_command} && {msim_executable_and_args}"
            
            update_job_status(job_id, "running_msim", "Executing MSIM command...", command=f"cd '{project_root_for_icenv}' && {shell_exec_command}")
            add_output_line_to_job(job_id, f"Executing shell command (within CWD {project_root_for_icenv}): {shell_exec_command}")
            add_output_line_to_job(job_id, "This may take some time...")

            process_return_code = None # Initialize here
            process_stderr_output = "" # Initialize here

            try: # INNER TRY for subprocess.Popen
                add_output_line_to_job(job_id, "Using inherited environment for subprocess.")
                add_output_line_to_job(job_id, f"Attempting to execute shell command with tcsh. Ensure 'tcsh' is in the inherited PATH.")
                
                process = subprocess.Popen(shell_exec_command, 
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                         text=True, bufsize=1, universal_newlines=True, 
                                         shell=True, executable='tcsh', cwd=project_root_for_icenv)
                
                if process.stdout:
                    for line in iter(process.stdout.readline, ''): 
                        print(line, end='') 
                        add_output_line_to_job(job_id, line.strip())
                    process.stdout.close()
                
                process_return_code = process.wait()
                
                if process.stderr:
                    process_stderr_output = process.stderr.read()
                    process.stderr.close()
                    if process_stderr_output:
                        add_output_line_to_job(job_id, "Shell Stderr (includes msim stderr):")
                        for line_err in process_stderr_output.splitlines(): 
                            add_output_line_to_job(job_id, line_err.strip())
                
                final_status_key_inner = "completed" if process_return_code == 0 else "failed"
                final_status_message_inner = f"MSIM run (via shell with icenv) {'completed successfully' if process_return_code == 0 else f'failed with return code {process_return_code}'}."
                update_job_status(job_id, final_status_key_inner, final_status_message_inner, returncode=process_return_code, stderr=process_stderr_output if process_return_code != 0 else None)
                add_output_line_to_job(job_id, final_status_message_inner)

            except FileNotFoundError: # For INNER TRY (Popen)
                process_return_code = -1 # Indicate failure
                update_job_status(job_id, "failed", "Shell (tcsh) or core command (msim/icenv) not found. Ensure tcsh, icenv, and msim are accessible.")
                add_output_line_to_job(job_id, "Error: Shell (tcsh) or essential command (msim/icenv) not found. Check PATH and icenv setup.")
            except Exception as e_popen: # For INNER TRY (Popen)
                process_return_code = -1 # Indicate failure
                update_job_status(job_id, "failed", f"An error occurred during shell (msim with icenv) execution: {e_popen}")
                add_output_line_to_job(job_id, f"Error during shell (msim with icenv) execution: {str(e_popen)}")

            # This code runs AFTER the inner try-except for Popen, still inside the OUTER try
            full_msim_stdout = "\n".join(JOB_STATUS[job_id].get("output_lines", []))
            proj_root_dir_for_logs = project_root_for_icenv 
            actual_sim_root_for_parsing = None; base_log_path_for_html = None; log_path_error = False

            if not proj_root_dir_for_logs:
                add_output_line_to_job(job_id, "CRITICAL Error: Project root not available for log parsing.")
                log_path_error = True
            else:
                # ... (vcs_context_basename determination) ...
                vcs_context_from_client_for_log_parsing = options.get('vcsContext', '')
                vcs_context_basename = os.path.basename(vcs_context_from_client_for_log_parsing) if vcs_context_from_client_for_log_parsing else ""
                if not vcs_context_basename:
                    current_full_branch_path_for_log_parsing = options.get('branchPath', '')
                    if current_full_branch_path_for_log_parsing:
                        vcs_context_basename = os.path.basename(current_full_branch_path_for_log_parsing)
                # ... (actual_sim_root_for_parsing and base_log_path_for_html determination) ...
                if final_msim_dir_option: 
                    actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename, "sim")
                    base_log_path_for_html = os.path.join("work", final_msim_dir_option, vcs_context_basename)
                else:
                    current_full_branch_path_for_log_parsing = options.get('branchPath', '')
                    extracted_branch_suffix = ""
                    if "work/" in current_full_branch_path_for_log_parsing:
                        extracted_branch_suffix = current_full_branch_path_for_log_parsing.split("work/", 1)[1] 
                    if not extracted_branch_suffix: log_path_error = True
                    else:
                        actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", extracted_branch_suffix, "sim")
                        base_log_path_for_html = os.path.join("work", extracted_branch_suffix) 
            
            if not log_path_error:
                add_output_line_to_job(job_id, f"  Final calculated absolute sim root for parsing: {actual_sim_root_for_parsing}")
                add_output_line_to_job(job_id, f"  Final calculated base relative path for HTML logs: {base_log_path_for_html}")

            if log_path_error or not actual_sim_root_for_parsing or not os.path.isdir(actual_sim_root_for_parsing):
                 detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout, options.get('selectedCases', []), None, None, job_id)
            else:
                detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout, options.get('selectedCases', []), actual_sim_root_for_parsing, base_log_path_for_html, job_id)
            JOB_STATUS[job_id]['detailed_test_results'] = detailed_results
            add_output_line_to_job(job_id, f"Final detailed test results: {detailed_results}")

            if detailed_results and (JOB_STATUS[job_id]['status'] == "completed" or JOB_STATUS[job_id]['status'] == "failed"):
                original_dir_from_client = options.get('actualWorkDirFromFilePath')
                if project_root_for_icenv and original_dir_from_client and derived_ip_name:
                    update_html_report_on_disk(None, detailed_results, job_id, project_root_for_icenv, original_dir_from_client, derived_ip_name, logger_to_use_start)

                # ---- BEGIN: Update Repo DB object after rerun ----
                repo_id_to_update = options.get('url_repo_id')
                if repo_id_to_update and Repo and db: # Ensure Repo and db are available from imports
                    add_output_line_to_job(job_id, f"Attempting to update Repo DB object for repo_id: {repo_id_to_update} after rerun.")
                    logger_to_use_start.info(f"Job {job_id}: Attempting to update Repo DB object for repo_id: {repo_id_to_update} after rerun.")
                    try:
                        repo_to_update = Repo.query.get(repo_id_to_update)
                        if repo_to_update:
                            passed_count = sum(1 for r_stat in detailed_results if r_stat.get('status') == 'PASSED')
                            failed_count = sum(1 for r_stat in detailed_results if r_stat.get('status') == 'FAILED')
                            unknown_count = sum(1 for r_stat in detailed_results if r_stat.get('status') not in ['PASSED', 'FAILED'])
                            total_rerun_cases = len(detailed_results)

                            pass_rate_val = (passed_count / total_rerun_cases * 100) if total_rerun_cases > 0 else 0.0
                            pass_rate_str = f"{pass_rate_val:.2f}%"

                            repo_to_update.status = "rerun" # Update repo status
                            
                            new_summary_stats_for_repo_result = {
                                "passed": passed_count,
                                "failed": failed_count, # Explicitly FAILED cases
                                "unknown": unknown_count, # Explicitly UNKNOWN/OTHER cases
                                "total": total_rerun_cases,
                                "pass_rate": pass_rate_str
                            }
                            
                            # Safely update the .result JSON field, preserving other existing keys
                            current_repo_result_data = repo_to_update.result
                            if not isinstance(current_repo_result_data, dict):
                                current_repo_result_data = {} # Initialize if None or not a dict
                            
                            current_repo_result_data.update(new_summary_stats_for_repo_result) # Merge new stats
                            repo_to_update.result = current_repo_result_data # Assign back to ensure change detection

                            db.session.commit()
                            add_output_line_to_job(job_id, f"Repo DB object {repo_id_to_update} updated. Status: rerun, Results: {new_summary_stats_for_repo_result}")
                            logger_to_use_start.info(f"Job {job_id}: Repo DB object {repo_id_to_update} updated successfully.")

                            # Notify client that repo data itself has changed (for this specific job's initiator)
                            if socketio: # Check if socketio is available
                                socketio.emit('repo_status_update',  # Changed event name
                                              {'repo_id': repo_id_to_update, 
                                               'new_status': 'rerun', 
                                               'new_result_summary': new_summary_stats_for_repo_result},
                                              namespace='/rerun_jobs', 
                                              room=job_id) # Notify client that initiated this job
                                logger_to_use_start.info(f"Job {job_id}: Emitted 'repo_status_update' for repo {repo_id_to_update} to job room {job_id}.")

                        else:
                            add_output_line_to_job(job_id, f"Error: Repo DB object with id {repo_id_to_update} not found for update after rerun.")
                            logger_to_use_start.error(f"Job {job_id}: Repo DB object {repo_id_to_update} not found for update after rerun.")
                    except Exception as e_db_update:
                        db.session.rollback() # Rollback in case of error during DB update
                        add_output_line_to_job(job_id, f"Error updating Repo DB object {repo_id_to_update} after rerun: {str(e_db_update)}")
                        logger_to_use_start.error(f"Job {job_id}: Error updating Repo DB object {repo_id_to_update} after rerun: {str(e_db_update)}", exc_info=True)
                elif not (Repo and db): # If Repo or db were not imported successfully at the start
                     add_output_line_to_job(job_id, "Warning: Repo or db module not available. Skipping Repo DB object update after rerun.")
                     logger_to_use_start.warning(f"Job {job_id}: Repo or db module not available. Skipping Repo DB object update after rerun.")
                # ---- END: Update Repo DB object after rerun ----
            
            # ... (find_primary_log_for_rerun calls) ...
            if not log_path_error and proj_root_dir_for_logs and final_msim_dir_option and vcs_context_basename:
                rerun_output_base_abs = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename)
                find_primary_log_for_rerun(rerun_output_base_abs) # Result not stored, just logged by function
            elif not log_path_error and proj_root_dir_for_logs and base_log_path_for_html and not final_msim_dir_option :
                rerun_output_base_abs_fallback = os.path.join(proj_root_dir_for_logs, base_log_path_for_html)
                find_primary_log_for_rerun(rerun_output_base_abs_fallback) # Result not stored
            
            # End of the original main try block's content
            # except Exception as e_outer: # This was the original main exception handler
            #     logger_to_use_exc = current_app_logger if current_app_logger else main_flask_app.logger
            #     logger_to_use_exc.error(f"CRITICAL ERROR IN TASK {job_id} (WITH APP CONTEXT): {str(e_outer)}", exc_info=True) 
            #     update_job_status(job_id, "failed", f"Critical error in task: {str(e_outer)}")
            #     add_output_line_to_job(job_id, f"CRITICAL_TASK_ERROR: {str(e_outer)}")
            # finally: # This was the original main finally block
            #     logger_to_use_finally = current_app_logger if current_app_logger else main_flask_app.logger
            #     logger_to_use_finally.info(f"--- Task ended for job_id: {job_id} (WITH APP CONTEXT) ---")

    # New top-level exception handler for the entire function
    except Exception as e_very_outer:
        error_message_top = f"CRITICAL UNCAUGHT EXCEPTION IN TASK {job_id}: {str(e_very_outer)}"
        print(f"[THREAD_DEBUG] {error_message_top}") # Raw print for sure
        import traceback
        traceback.print_exc() # Print stack trace to console
        
        # Try to update job status
        try:
            update_job_status(job_id, "failed", f"Critical uncaught error: {str(e_very_outer)}")
            if "job_id_for_logging" not in options: options["job_id_for_logging"] = job_id # Ensure it's set
            add_output_line_to_job(job_id, error_message_top)
            add_output_line_to_job(job_id, traceback.format_exc())
        except Exception as e_status_update:
            print(f"[THREAD_DEBUG] job_id: {job_id} - Failed to update job status after top-level exception: {str(e_status_update)}")
        
        # Try to log using a logger if available from the app_context, otherwise just print
        try:
            with actual_flask_app_instance.app_context(): # Re-establish context if it was lost or never entered
                 logger_final_error = current_app_logger if current_app_logger else actual_flask_app_instance.logger
                 if logger_final_error:
                     logger_final_error.error(f"CRITICAL UNCAUGHT EXCEPTION IN TASK {job_id} (WITH APP CONTEXT): {str(e_very_outer)}", exc_info=True)
        except Exception as e_log_final:
            print(f"[THREAD_DEBUG] job_id: {job_id} - Failed to log final error using app logger: {str(e_log_final)}")
    finally:
        # This finally block is for the new top-level try
        print(f"[THREAD_DEBUG] long_running_rerun_task finished or exited for job_id: {job_id} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        # Ensure the logger from app_context is used if possible for the final message
        try:
            with actual_flask_app_instance.app_context(): # Re-establish context
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
            if repo_obj: project_base_path_from_db = getattr(repo_obj, 'data_path', None) 
        data = request.get_json()
        if not data or 'selectedCases' not in data: 
            return jsonify({"status": "error", "message": "Invalid request"}), 400
        data['url_repo_id'] = repo_id 
        if project_base_path_from_db: data['db_project_base_path'] = project_base_path_from_db
        
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
