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
from flask import render_template, request, jsonify, send_from_directory, Blueprint, Flask
from flask_cors import CORS # Added CORS import
# Attempt to import database models and extension, fail gracefully if not available for standalone use
try:
    from models import Repo
    from extensions import db
except ImportError:
    Repo = None
    db = None
    print("Warning: 'models' or 'extensions' module not found. Database features will be disabled.")

# base_dir for templates, assuming a project structure where 'templates' is two levels up from this file's dir, then down into 'templates'
# This might need adjustment based on actual project structure if this script is moved.
# For AI_HTML/live_report_server_new.py, os.path.abspath(__file__) is .../AI_HTML/live_report_server_new.py
# os.path.dirname(os.path.abspath(__file__)) is .../AI_HTML
# os.path.dirname(os.path.dirname(os.path.abspath(__file__))) is .../ (the parent of AI_HTML)
# This assumes templates are in .../templates. If AI_HTML is a blueprint part of a larger app, this is plausible.
_current_file_dir = os.path.dirname(os.path.abspath(__file__))
_project_root_approx = os.path.dirname(_current_file_dir) # Parent of AI_HTML
base_dir_for_templates = os.path.join(_project_root_approx, 'templates')

bp = Blueprint('live_reporter', __name__, template_folder=base_dir_for_templates)

script_dir = os.path.dirname(os.path.abspath(__file__)) # This is AI_HTML directory
JOB_STATUS = {} # Stores status, message, command, returncode, stdout, stderr, output_lines

# --- Utility functions for job status ---
def update_job_status(job_id, status, message=None, command=None, returncode=None, stdout=None, stderr=None):
    if job_id not in JOB_STATUS: # Ensure job_id entry exists
        JOB_STATUS[job_id] = {"output_lines": [], "status": "initializing", "message": "Job initializing."}
    
    JOB_STATUS[job_id]['status'] = status
    if message is not None: JOB_STATUS[job_id]['message'] = message
    if command is not None: JOB_STATUS[job_id]['command'] = command
    if returncode is not None: JOB_STATUS[job_id]['returncode'] = returncode
    if stdout is not None: JOB_STATUS[job_id]['stdout'] = stdout
    if stderr is not None: JOB_STATUS[job_id]['stderr'] = stderr
    # output_lines are handled by add_output_line_to_job

def add_output_line_to_job(job_id, line):
    if job_id not in JOB_STATUS:
        JOB_STATUS[job_id] = {"output_lines": [], "status": "unknown", "message": "Job initialized by output line."}
    elif "output_lines" not in JOB_STATUS[job_id]:
         JOB_STATUS[job_id]["output_lines"] = []
    JOB_STATUS[job_id]["output_lines"].append(line)

    # --- Live progress parsing ---
    if 'progress_summary' in JOB_STATUS[job_id] and JOB_STATUS[job_id].get('status') == 'running_msim':
        # Example: UVM_INFO ... [TEST_DONE] Test SimplePingTest_seed123 (PASSED)
        uvm_test_done_pattern = re.compile(r"\[TEST_DONE\]\s*Test\s*([\w_.-]+seed\d+)\s*\((\w+)\)")
        match = uvm_test_done_pattern.search(line)
        if match:
            status_from_log = match.group(2).upper()
            summary = JOB_STATUS[job_id]['progress_summary']
            
            # This simple increment assumes each TEST_DONE is unique for a test during live parse.
            # More sophisticated logic might be needed if tests can have multiple TEST_DONE lines or if parsing needs to be idempotent.
            # We only increment if we haven't processed all selected tests yet.
            if summary['processed_count'] < summary['total_selected']:
                summary['processed_count'] += 1
                if status_from_log == "PASSED":
                    summary['passed_count'] += 1
                elif status_from_log == "FAILED": # Or other failure states like UVM_FATAL, etc.
                    summary['failed_count'] += 1
                # else: status is neither PASSED nor FAILED (e.g. some other UVM status),
                # it's counted as processed but doesn't change pass/fail counts here.
                # The final parse_msim_output_for_test_statuses will give definitive status.
                # print(f"Job {job_id} live progress update: {summary}") # For server-side debugging
    # --- End Live progress parsing ---

def get_job_status(job_id):
    return JOB_STATUS.get(job_id, {"status": "not_found", "message": "Job ID not found.", "output_lines": []})
# --- End Utility functions ---

def get_project_root_from_branch_path(branch_path, job_id_for_logging=None):
    """
    Derives the project root directory (e.g., /path/to/PRJ_ICDIR) from a full branch path.
    The project root is assumed to be the part of the path before '/work/'.
    """
    if not branch_path or '/work/' not in branch_path:
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: branchPath '{branch_path}' is invalid or does not contain '/work/'. Cannot determine project root.")
        return None
    project_root = branch_path.split('/work/', 1)[0]
    if job_id_for_logging:
        add_output_line_to_job(job_id_for_logging, f"Derived project root for icenv and HJSON base: {project_root} from branchPath: {branch_path}")
    return project_root

def _parse_individual_parse_run_log(parse_run_log_path):
    """Helper to parse a single parse_run.log file based on its first line."""
    try:
        with open(parse_run_log_path, 'r', encoding='utf-8', errors='replace') as f:
            first_line = f.readline().strip().lower()
            if 'run.log passed' in first_line:
                return 'PASSED'
            elif 'run.log failed' in first_line or 'run.log is unknown' in first_line:
                return 'FAILED'
            else:
                # If parse_run.log exists but content is unexpected for pass/fail
                return 'UNKNOWN' 
    except FileNotFoundError:
        return None # Indicates parse_run.log was not found
    except Exception as e:
        # Log an error if parsing fails for an existing file
        print(f"Error parsing log file {parse_run_log_path}: {e}")
        return 'UNKNOWN' # Indicates error during parsing

def find_primary_log_for_rerun(base_search_path):
    """
    Searches for run.log or comp.log within the base_search_path.
    Priority: run.log (common locations, then deep search), then comp.log (common, then deep).
    Returns the absolute path to the found log file, or None.
    """
    if not base_search_path or not os.path.isdir(base_search_path):
        print(f"Warning: Base search path for logs is invalid or not a directory: {base_search_path}")
        return None
    log_filenames_priority = ["run.log", "comp.log"]
    common_subdirs = ["", "latest"] # Check in base_search_path itself, and in base_search_path/latest/
    for log_filename in log_filenames_priority:
        # Check common locations first
        for subdir in common_subdirs:
            potential_log_path = os.path.join(base_search_path, subdir, log_filename)
            if os.path.exists(potential_log_path):
                print(f"Found log '{log_filename}' in common location: {os.path.abspath(potential_log_path)}")
                return os.path.abspath(potential_log_path)
        # If not in common locations, perform a wider search (os.walk)
        print(f"Log '{log_filename}' not in common locations. Starting deeper search in {base_search_path}...")
        for root, _, files in os.walk(base_search_path):
            if log_filename in files:
                found_path = os.path.abspath(os.path.join(root, log_filename))
                print(f"Found log '{log_filename}' via os.walk: {found_path}")
                return found_path
        print(f"Log '{log_filename}' not found via os.walk in {base_search_path}.")
    print(f"No run.log or comp.log found in {base_search_path} after checking common locations and deep search.")
    return None

def parse_msim_output_for_test_statuses(msim_stdout, selected_cases_with_seed,
                                        actual_sim_root_for_parsing, # Absolute path to the .../sim/ directory
                                        base_log_path_for_html,      # Relative path prefix for HTML links, e.g., work/.../vcs-context
                                        job_id_for_logging=None):
    """
    Parses MSIM stdout and individual test logs to extract final status for each test.
    actual_sim_root_for_parsing: Absolute path to the directory containing individual case_id_variant simulation dirs (e.g., /path/to/PRJ_ICDIR/work/.../vcs-context/sim).
                                 If None or invalid, parsing individual logs will be skipped.
    base_log_path_for_html: The base relative path used for constructing HTML log links (e.g., "work/report_dir/mtu-vcs" or "work/user_dir_opt/mtu-vcs").
    Returns a list of dictionaries: [{'id': 'test_case_name_seedXXXX', 'status': 'PASSED'/'FAILED'/'UNKNOWN',
                                     'error_hint': '...', 'new_log_path': 'relative/path/to/new/run.log'}]
    """
    results_map = {}

    if job_id_for_logging:
        add_output_line_to_job(job_id_for_logging, f"Starting detailed status parsing. Sim root for parsing: {actual_sim_root_for_parsing}, Base HTML log path: {base_log_path_for_html}")

    if not base_log_path_for_html and actual_sim_root_for_parsing: # Should not happen if called correctly
         add_output_line_to_job(job_id_for_logging, "Warning: base_log_path_for_html is missing, HTML log paths might be incorrect.")
         # Attempt to derive a fallback, this is risky
         # base_log_path_for_html = os.path.basename(os.path.dirname(actual_sim_root_for_parsing)) # e.g., vcs-context if path is .../vcs-context/sim

    for case_id in selected_cases_with_seed:
        current_status = "UNKNOWN"
        error_hint = "Status not determined."
        
        safe_base_html_path = base_log_path_for_html.replace(os.sep, '/') if base_log_path_for_html else "unknown_html_base"
        # Default HTML log path (fallback if specific case variant dir or log isn't found)
        # Uses the original case_id and 'latest'. Ensure forward slashes for HTML.
        html_log_path = f"{safe_base_html_path}/sim/{case_id}/latest/run.log" # Default fallback

        case_id_variant_dir_name = None
        individual_test_sim_dir_actual = None # This will be .../sim/<case_id_variant>/

        if actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
            try:
                found_match = False
                # Sort to get a predictable order if multiple startswith matches (e.g. case, case.0, case.1)
                # Prefer shorter matches if that's a convention, or specific ones like case_id itself first.
                # For now, simple startswith, first found.
                dir_items = sorted(os.listdir(actual_sim_root_for_parsing)) 
                for item_name in dir_items:
                    item_path = os.path.join(actual_sim_root_for_parsing, item_name)
                    if os.path.isdir(item_path) and item_name.startswith(case_id):
                        case_id_variant_dir_name = item_name
                        individual_test_sim_dir_actual = item_path
                        if job_id_for_logging:
                            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Found matching sim directory: {case_id_variant_dir_name} at {individual_test_sim_dir_actual}")
                        found_match = True
                        break # Take the first match
                if not found_match and job_id_for_logging:
                     add_output_line_to_job(job_id_for_logging, f"For {case_id}: No directory starting with '{case_id}' found in '{actual_sim_root_for_parsing}'.")
            except Exception as e:
                if job_id_for_logging:
                    add_output_line_to_job(job_id_for_logging, f"For {case_id}: Error listing or processing sim dirs in '{actual_sim_root_for_parsing}': {e}")
            
            if individual_test_sim_dir_actual and os.path.isdir(individual_test_sim_dir_actual):
                latest_log_dir = os.path.join(individual_test_sim_dir_actual, 'latest')
                if not os.path.isdir(latest_log_dir):
                    if job_id_for_logging:
                        add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): 'latest' symlink not found. Searching for newest timestamped directory...")
                    subdirs = [d for d in os.listdir(individual_test_sim_dir_actual) if os.path.isdir(os.path.join(individual_test_sim_dir_actual, d))]
                    if subdirs:
                        # Filter out known non-log dirs if any, e.g. 'waves'
                        # For now, assume all subdirs are potential log dirs
                        timestamped_subdirs = [os.path.join(individual_test_sim_dir_actual, d) for d in subdirs]
                        if timestamped_subdirs:
                            latest_log_dir = max(timestamped_subdirs, key=os.path.getmtime)
                            if job_id_for_logging:
                                add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): Found newest timestamped dir: {os.path.basename(latest_log_dir)}")
                        else: latest_log_dir = None
                    else: latest_log_dir = None
                
                if latest_log_dir and os.path.isdir(latest_log_dir):
                    parse_run_log_path = os.path.join(latest_log_dir, 'parse_run.log')
                    # actual_run_log_path = os.path.join(latest_log_dir, 'run.log') # For reference
                    
                    timestamp_or_latest_name = os.path.basename(latest_log_dir)
                    # Use case_id_variant_dir_name for the HTML path. Ensure forward slashes.
                    html_log_path = f"{safe_base_html_path}/sim/{case_id_variant_dir_name.replace(os.sep, '/')}/{timestamp_or_latest_name.replace(os.sep, '/')}/run.log"
                    
                    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): Checking parse_run.log at {parse_run_log_path}")
                    status_from_parse_log = _parse_individual_parse_run_log(parse_run_log_path)
                    if status_from_parse_log: # Check if status_from_parse_log is not None
                        current_status = status_from_parse_log
                        error_hint = "Failed (from parse_run.log)" if current_status == "FAILED" else ("" if current_status == "PASSED" else "Status unclear from parse_run.log")
                        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from parse_run.log: {current_status}")
                elif job_id_for_logging: # This block executes if latest_log_dir is not valid
                    add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name if case_id_variant_dir_name else 'N/A'}): 'latest' or timestamped log directory not resolved or not a directory: {latest_log_dir}")
            elif job_id_for_logging and actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
                 # This case means individual_test_sim_dir_actual was not found/set
                 add_output_line_to_job(job_id_for_logging, f"For {case_id}: Could not find or access specific simulation directory for this case variant.")
        
        elif job_id_for_logging: # actual_sim_root_for_parsing is None or not a dir
            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Main sim root for parsing ('{actual_sim_root_for_parsing}') is not valid. Skipping individual log check.")

        if current_status == "UNKNOWN":
            if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: parse_run.log status is UNKNOWN or file not found. Checking msim_stdout.")
            # Regex for specific test_id (case_id format is test_base_name_seedSEED)
            # The [TEST_DONE] log uses the full name including _seedSEED
            uvm_test_done_pattern_specific = re.compile(r"\[TEST_DONE\]\s*Test\s*" + re.escape(case_id) + r"\s*\((\w+)\)")
            for line in msim_stdout.splitlines():
                match = uvm_test_done_pattern_specific.search(line)
                if match:
                    status_from_stdout = match.group(1).upper()
                    current_status = status_from_stdout # Override if found
                    error_hint = "Failed (from [TEST_DONE] in msim stdout)" if status_from_stdout == "FAILED" else ("" if status_from_stdout == "PASSED" else error_hint)
                    if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from msim_stdout [TEST_DONE]: {current_status}")
                    break
        
        results_map[case_id] = {"id": case_id, "status": current_status, "error_hint": error_hint, "new_log_path": html_log_path}
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"For {case_id}: Final determined status: {current_status}, Log: {html_log_path}")
    return list(results_map.values())

def prepare_rerun_hjson_files(project_root_for_hjson, options, temp_rerun_dir, ip_name): # Added project_root_for_hjson
    # This function is from S_latest (and S_old), seems unchanged by S_change's Blueprint modifications directly.
    # It uses job_id_for_logging passed in options, which is good.
    print(f"--- prepare_rerun_hjson_files called for IP: {ip_name} ---")
    print(f"Using project_root_for_hjson: {project_root_for_hjson}") # New log
    job_id_for_logging = options.get("job_id_for_logging")
    # proj_root_dir = os.environ.get('PRJ_ICDIR') # OLD
    proj_root_dir = project_root_for_hjson # NEW
    if not proj_root_dir:
        error_msg = "CRITICAL ERROR: Project root directory was not provided or derived. Cannot locate original HJSON files." # Updated error
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, "Error: Project root directory not available. Configure server environment or check branchPath.") # Updated error
        return None
    # print(f"Using PRJ_ICDIR from environment: {proj_root_dir}") # OLD log
    original_hjson_filename = f"{ip_name}.hjson"
    original_hjson_path = os.path.join(proj_root_dir, "dv", "sim_ctrl", "ts", original_hjson_filename)
    print(f"Calculated original HJSON path: {original_hjson_path}")
    target_hjson_dir = os.path.join(proj_root_dir, "dv", "sim_ctrl", "ts", "temp")
    print(f"Target directory for 'rerun.hjson' under PRJ_ICDIR: {target_hjson_dir}")
    try:
        os.makedirs(target_hjson_dir, exist_ok=True)
        print(f"Ensured target directory for 'rerun.hjson' exists: {target_hjson_dir}")
    except Exception as e:
        error_msg = f"CRITICAL ERROR: Failed to create target directory {target_hjson_dir} for 'rerun.hjson': {e}"
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to create target directory {target_hjson_dir}: {e}")
        return None
    temp_target_hjson_path = os.path.join(target_hjson_dir, "rerun.hjson")
    print(f"Temporary target HJSON path for copy: {temp_target_hjson_path}")
    if not os.path.exists(original_hjson_path):
        error_msg = f"CRITICAL ERROR: Source HJSON file does not exist at the calculated path: {original_hjson_path}"
        print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Source HJSON not found: {original_hjson_path}")
        return None
    print(f"Source HJSON file found at {original_hjson_path}")
    try:
        shutil.copy(original_hjson_path, temp_target_hjson_path)
        print(f"Successfully copied {original_hjson_path} to {temp_target_hjson_path}")
    except Exception as e:
        error_msg = f"Error: Could not copy HJSON file from {original_hjson_path} to {temp_target_hjson_path}: {e}"
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
    final_tests_section_for_output = []
    test_names_for_regression_list = []
    original_tests_from_hjson = target_hjson_data.get("tests", [])
    original_test_defs_map_by_base_name = {}
    if isinstance(original_tests_from_hjson, list):
        print(f"Info: Original 'tests' section in {temp_target_hjson_path} is a list. Parsing for templates.")
        for test_def in original_tests_from_hjson:
            if isinstance(test_def, dict) and "name" in test_def: original_test_defs_map_by_base_name[test_def["name"]] = test_def
            else: print(f"Warning: Malformed item in original 'tests' list: {test_def}. Skipping.")
    elif isinstance(original_tests_from_hjson, dict):
        print(f"Info: Original 'tests' section in {temp_target_hjson_path} is a dictionary. Parsing for templates.")
        for base_name, test_def in original_tests_from_hjson.items():
            if isinstance(test_def, dict): original_test_defs_map_by_base_name[base_name] = test_def
            else: print(f"Warning: Malformed item in original 'tests' dict for key '{base_name}': {test_def}. Skipping.")
    else:
        print(f"Warning: Original 'tests' section in {temp_target_hjson_path} is neither list nor dict (type: {type(original_tests_from_hjson)}). Cannot find base test definitions for templates.")
    selected_cases_for_this_ip = [case_id for case_id in options.get('selectedCases', []) if case_id.startswith(ip_name + "_")]
    if not selected_cases_for_this_ip:
        print(f"Info: No cases selected for IP '{ip_name}'. 'tests' section in rerun.hjson will be empty.")
    else:
        for case_id_with_seed in selected_cases_for_this_ip:
            parts = case_id_with_seed.split("_seed")
            if len(parts) != 2: print(f"Warning: Could not parse base name and seed from '{case_id_with_seed}'. Skipping this case for HJSON."); continue
            base_test_name, seed_str = parts[0], parts[1]
            try: seed_val = int(seed_str)
            except ValueError: print(f"Warning: Invalid seed value '{seed_str}' in '{case_id_with_seed}'. Skipping this case for HJSON."); continue
            original_def_template = original_test_defs_map_by_base_name.get(base_test_name)
            new_test_def_object = copy.deepcopy(original_def_template) if original_def_template else {"uvm_test_seq": f"unknown_vseq_for_{base_test_name}", "build_mode": f"unknown_build_mode_for_{base_test_name}"}
            if original_def_template: print(f"Info: Found original definition template for base test '{base_test_name}'.")
            else: print(f"Warning: Original definition template for base test '{base_test_name}' not found. Creating a minimal definition for rerun.")
            new_test_def_object['name'] = case_id_with_seed
            if 'seed' in new_test_def_object: del new_test_def_object['seed']
            current_run_opts = new_test_def_object.get("run_opts", [])
            if not isinstance(current_run_opts, list): print(f"Warning: 'run_opts' for base test '{base_test_name}' was not a list. Re-initializing."); current_run_opts = []
            updated_run_opts = [opt for opt in current_run_opts if not str(opt).startswith("+ntb_random_seed=")]
            updated_run_opts.append(f"+ntb_random_seed={seed_val}")
            new_test_def_object['run_opts'] = updated_run_opts
            print(f"Info: Updated 'run_opts' for test '{case_id_with_seed}' to include '+ntb_random_seed={seed_val}'.")
            final_tests_section_for_output.append(new_test_def_object)
            test_names_for_regression_list.append(case_id_with_seed)
            print(f"Info: Prepared test definition for '{case_id_with_seed}' to be included in rerun.hjson 'tests' list.")
    target_hjson_data['tests'] = final_tests_section_for_output
    print(f"Info: 'tests' section of rerun.hjson will now be a list with {len(final_tests_section_for_output)} test definition objects.")
    rerun_regression_group = {"name": "rerun", "tests": test_names_for_regression_list}
    if not isinstance(target_hjson_data.get("regressions"), list):
        print(f"Warning: 'regressions' section in loaded HJSON is not a list. Initializing as new list.")
        target_hjson_data["regressions"] = [rerun_regression_group]
    else:
        existing_rerun_index = next((i for i, reg in enumerate(target_hjson_data["regressions"]) if isinstance(reg, dict) and reg.get("name") == "rerun"), None)
        if existing_rerun_index is not None: print("Info: Updating existing 'rerun' regression group."); target_hjson_data["regressions"][existing_rerun_index] = rerun_regression_group
        else: print("Info: Adding new 'rerun' regression group."); target_hjson_data["regressions"].append(rerun_regression_group)
    print(f"Info: Final 'rerun' regression group's 'tests' list: {test_names_for_regression_list}")
    try:
        with open(temp_target_hjson_path, 'w') as file: hjson.dump(target_hjson_data, file, indent=2)
        print(f"Successfully wrote modified HJSON to {temp_target_hjson_path}")
        return temp_target_hjson_path
    except Exception as e:
        error_msg = f"Error: Could not write modified HJSON to {temp_target_hjson_path}: {e}"; print(error_msg)
        if job_id_for_logging: add_output_line_to_job(job_id_for_logging, f"Error: Failed to write HJSON {temp_target_hjson_path}: {e}")
        return None

def long_running_rerun_task(job_id, options, current_app_logger): # Added current_app_logger
    # Using current_app_logger instead of app.logger or bp.logger directly
    current_app_logger.info(f"--- Starting long_running_rerun_task for job_id: {job_id} ---")
    current_app_logger.info(f"Options received by task: {options}")
    try:
        options["job_id_for_logging"] = job_id
        
        # --- Get project_root_for_icenv ---
        branch_path_for_root_derivation = options.get('branchPath') # This should be the H3 full path
        project_root_for_icenv = get_project_root_from_branch_path(branch_path_for_root_derivation, job_id)

        if not project_root_for_icenv:
            # get_project_root_from_branch_path already logs the error to job status
            update_job_status(job_id, "failed", "Failed to derive project root from branchPath. Cannot proceed.")
            return
        # --- End Get project_root_for_icenv ---

        num_selected_cases = len(options.get('selectedCases', []))
        JOB_STATUS[job_id]['progress_summary'] = {"total_selected": num_selected_cases, "processed_count": 0, "passed_count": 0, "failed_count": 0}
        update_job_status(job_id, "preparing_hjson", "Preparing HJSON files...")
        add_output_line_to_job(job_id, "Rerun task started. Preparing HJSON files...")
        temp_rerun_dir_name = f"temp_rerun_{job_id}_{str(uuid.uuid4())[:8]}"
        temp_rerun_dir = os.path.join(script_dir, temp_rerun_dir_name) # script_dir is AI_HTML
        try:
            os.makedirs(temp_rerun_dir, exist_ok=True)
            add_output_line_to_job(job_id, f"Created temporary directory for rerun: {temp_rerun_dir}")
        except Exception as e:
            update_job_status(job_id, "failed", f"Failed to create temp directory: {e}")
            add_output_line_to_job(job_id, f"Error: Failed to create temporary directory {temp_rerun_dir}: {e}")
            return
        
        # branch_path here is used for IP name derivation, might be different from the one for root derivation
        # Client sends 'branchPath' which is vcsContext (e.g., "ip_name-vcs") for IP derivation
        # And also sends 'fullH3BranchPath' (not currently used by this script, but was used by live_report_server.py for root)
        # For now, assume options.get('branchPath') is the vcsContext for IP derivation.
        # The root derivation uses options.get('branchPath') which should be the H3 full path.
        # This needs clarification if client sends two different branchPath fields.
        # Assuming 'branchPath' in options is the one for IP derivation (e.g., "mtu-vcs")
        # And the one used for get_project_root_from_branch_path was also options.get('branchPath')
        # This implies options.get('branchPath') must be the H3 full path.
        
        ip_derivation_path = options.get('branchPath') # This is the H3 full path, used for IP name too.
        if not ip_derivation_path: 
            update_job_status(job_id, "failed", "Branch path (for IP derivation) not provided by client.")
            add_output_line_to_job(job_id, "Error: Branch path (for IP derivation) is missing. Cannot determine IP for HJSON.")
            return
        add_output_line_to_job(job_id, f"Received branch path for IP context: {ip_derivation_path}")
        
        derived_ip_name = None
        try: 
            # Assuming ip_derivation_path is the H3 full path like /gsa/pokgsa/projects/m/mtu_proj/work/user_a/mtu-vcs
            # We need the basename, e.g., "mtu-vcs", then "mtu"
            ip_folder_name = os.path.basename(ip_derivation_path) 
            derived_ip_name = ip_folder_name.split('-', 1)[0]
            if not derived_ip_name: raise ValueError("Derived IP name is empty.")
        except Exception as e:
            update_job_status(job_id, "failed", f"Failed to derive IP name from branch path: {ip_derivation_path}. Error: {e}")
            add_output_line_to_job(job_id, f"Error: Could not derive IP name from branch path '{ip_derivation_path}': {e}")
            return
        add_output_line_to_job(job_id, f"Derived IP name for HJSON context: {derived_ip_name}")
        ip_names_to_process = {derived_ip_name}
        generated_hjson_paths_map = {}
        all_hjson_prepared_successfully = True
        for ip_name in ip_names_to_process:
            add_output_line_to_job(job_id, f"Processing IP: {ip_name} for HJSON preparation using project root: {project_root_for_icenv}")
            hjson_path = prepare_rerun_hjson_files(project_root_for_icenv, options, temp_rerun_dir, ip_name) # Pass project_root_for_icenv
            if hjson_path:
                generated_hjson_paths_map[ip_name] = hjson_path
                add_output_line_to_job(job_id, f"Successfully prepared HJSON for {ip_name} at {hjson_path}")
            else:
                add_output_line_to_job(job_id, f"Error: Failed to prepare HJSON for IP: {ip_name}.")
                update_job_status(job_id, "failed", f"Failed to prepare HJSON for {ip_name}.")
                all_hjson_prepared_successfully = False; break
        if not all_hjson_prepared_successfully: add_output_line_to_job(job_id, "HJSON prep failed. Aborting."); return
        if not generated_hjson_paths_map: update_job_status(job_id, "failed", "No HJSON files generated."); add_output_line_to_job(job_id, "Error: No HJSON files generated."); return
        update_job_status(job_id, "hjson_prepared", "HJSON files prepared. Starting MSIM...")
        add_output_line_to_job(job_id, "All HJSON files prepared successfully.")
        prepared_hjson_actual_path = list(generated_hjson_paths_map.values())[0]
        add_output_line_to_job(job_id, f"MSIM will use 'rerun' config, expecting HJSON at: {prepared_hjson_actual_path}")
        msim_command_parts = ["msim", "rerun", "-t", "rerun"]
        add_output_line_to_job(job_id, f"Base msim command: msim rerun -t rerun")
        if not options.get('rebuildCases', False): msim_command_parts.append("-so"); add_output_line_to_job(job_id, "Adding -so (skip optimize)")
        else: add_output_line_to_job(job_id, "Rebuilding all selected cases (no -so flag)")
        if options.get('includeWaveform'): msim_command_parts.append("-w"); add_output_line_to_job(job_id, "Adding -w (include waveform)")
        if options.get('openCoverage'): msim_command_parts.append("-c"); add_output_line_to_job(job_id, "Adding -c (open coverage)")
        sim_time_hours_str = options.get('simTimeHours', "0")
        try:
            sim_time_hours = int(sim_time_hours_str)
            if sim_time_hours > 0: sim_time_minutes = sim_time_hours * 60; msim_command_parts.extend(["-rto", str(sim_time_minutes)]); add_output_line_to_job(job_id, f"Adding -rto {sim_time_minutes}")
        except ValueError: add_output_line_to_job(job_id, f"Warning: Invalid simTimeHours: {sim_time_hours_str}. Not adding -rto.")

        # Determine the final -dir option for msim command
        user_specified_dir_option = options.get('dirOption', '').strip()
        full_branch_path_for_msim_dir_derivation = options.get('branchPath', '') # Used if user_specified_dir_option is empty
        final_msim_dir_option = None

        if user_specified_dir_option:
            final_msim_dir_option = user_specified_dir_option
            add_output_line_to_job(job_id, f"User specified -dir: '{final_msim_dir_option}'. This will be used for msim command.")
        else:
            add_output_line_to_job(job_id, "User did not specify -dir. Attempting to derive from branchPath.")
            if "work/" in full_branch_path_for_msim_dir_derivation:
                path_after_work = full_branch_path_for_msim_dir_derivation.split("work/", 1)[1] # e.g., "msim_report_v3p0_2/mtu-vcs"
                derived_dir_for_msim = os.path.normpath(path_after_work).split(os.sep)[0]
                if derived_dir_for_msim and derived_dir_for_msim != '.' and derived_dir_for_msim != os.path.basename(path_after_work):
                    final_msim_dir_option = derived_dir_for_msim
                    add_output_line_to_job(job_id, f"Derived msim -dir from branchPath ('{full_branch_path_for_msim_dir_derivation}') as: '{final_msim_dir_option}'.")
                else:
                    add_output_line_to_job(job_id, f"Warning: Could not derive a valid directory for msim -dir from branchPath '{full_branch_path_for_msim_dir_derivation}' (path after work: '{path_after_work}', derived: '{derived_dir_for_msim}').")
            else:
                add_output_line_to_job(job_id, f"Warning: 'work/' not found in branchPath '{full_branch_path_for_msim_dir_derivation}'. Cannot derive implicit -dir for msim.")

        if final_msim_dir_option:
            msim_command_parts.extend(["-dir", final_msim_dir_option])
        else:
            add_output_line_to_job(job_id, "MSIM command will not include an explicit -dir option (neither user-specified nor successfully derived).")

        elab_opts_value = options.get('elabOpts', '').strip()
        if elab_opts_value: msim_command_parts.extend(["-elab", elab_opts_value]); add_output_line_to_job(job_id, f"Adding -elab \"{elab_opts_value}\"")
        vlogan_opts_value = options.get('vloganOpts', '').strip()
        if vlogan_opts_value: msim_command_parts.extend(["-vlog", vlogan_opts_value]); add_output_line_to_job(job_id, f"Adding -vlog \"{vlogan_opts_value}\"")
        run_opts_value = options.get('runOpts', '').strip()
        if run_opts_value: msim_command_parts.extend(["-ro", run_opts_value]); add_output_line_to_job(job_id, f"Adding -ro \"{run_opts_value}\"")
        
        msim_executable_and_args = " ".join(msim_command_parts) # This is the actual msim command and its arguments
        icenv_script_path = "/remote/public/scripts/icenv.csh" # Standard path for icenv.csh
        
        # Construct the shell command to source .cshrc, then icenv.csh, then run msim
        shell_exec_command = f"source ~/.cshrc && {icenv_script_path} && {msim_executable_and_args}"
        
        update_job_status(job_id, "running_msim", "Executing MSIM command (sourcing ~/.cshrc, then icenv.csh, with full env inherit)...", command=f"cd '{project_root_for_icenv}' && {shell_exec_command}") # Log original intent
        add_output_line_to_job(job_id, f"Setting CWD for subprocess to: {project_root_for_icenv}") # project_root_for_icenv was derived earlier
        add_output_line_to_job(job_id, f"Executing shell command (within CWD): {shell_exec_command}")
        add_output_line_to_job(job_id, "This may take some time...")
        
        try:
            # Using inherited environment from the parent (Python script).
            add_output_line_to_job(job_id, "Using inherited environment for subprocess.")
            add_output_line_to_job(job_id, f"Attempting to execute shell command with tcsh. Ensure 'tcsh' is in the inherited PATH.")
            
            process = subprocess.Popen(shell_exec_command, 
                                     stdout=subprocess.PIPE, 
                                     stderr=subprocess.PIPE, 
                                     text=True, 
                                     bufsize=1, 
                                     universal_newlines=True, 
                                     shell=True, 
                                     executable='tcsh',
                                     cwd=project_root_for_icenv) # env is inherited, cwd is set
            
            if process.stdout:
                for line in iter(process.stdout.readline, ''): 
                    print(line, end='') # Keep server console log if needed
                    add_output_line_to_job(job_id, line.strip())
                process.stdout.close()
            
            return_code = process.wait()
            
            stderr_output = ""
            if process.stderr:
                stderr_output = process.stderr.read()
                process.stderr.close()
                if stderr_output:
                    add_output_line_to_job(job_id, "Shell Stderr (includes msim stderr):") # Updated label
                    for line_err in stderr_output.splitlines(): 
                        add_output_line_to_job(job_id, line_err.strip())
            
            final_status_key = "completed" if return_code == 0 else "failed"
            final_status_message = "MSIM run (via shell with icenv) completed successfully." if return_code == 0 else f"MSIM run (via shell with icenv) failed with return code {return_code}." # Updated message
            update_job_status(job_id, final_status_key, final_status_message, returncode=return_code, stderr=stderr_output if return_code != 0 else None)
            add_output_line_to_job(job_id, final_status_message)
            full_msim_stdout = "\n".join(JOB_STATUS[job_id].get("output_lines", [])) # This now contains output from tcsh, icenv, and msim
            
            # Determine paths for log parsing and HTML links
            # This uses `final_msim_dir_option` (determined before msim command) and `vcs_context` from client.
            # proj_root_dir = os.environ.get('PRJ_ICDIR') # OLD
            # Use project_root_for_icenv derived earlier
            proj_root_dir_for_logs = project_root_for_icenv 

            actual_sim_root_for_parsing = None 
            base_log_path_for_html = None      
            log_path_error = False

            if not proj_root_dir_for_logs: # Check the derived project root
                add_output_line_to_job(job_id, "CRITICAL Error: Project root (derived from branchPath) is not available for log parsing.")
                log_path_error = True
            else:
                # Determine vcs_context_basename (e.g., "mtu-vcs")
                # options.get('branchPath') is assumed to be the H3 full path.
                # options.get('vcsContext') might be a shorter version like "mtu-vcs" from client.
                # Prefer vcsContext if available and seems valid, otherwise derive from branchPath.
                vcs_context_from_client_for_log_parsing = options.get('vcsContext', '') # This is what live_report_server.py uses
                vcs_context_basename = os.path.basename(vcs_context_from_client_for_log_parsing) if vcs_context_from_client_for_log_parsing else ""
                
                if not vcs_context_basename:
                    # Fallback: try to derive from full_branch_path if vcsContext option was empty
                    current_full_branch_path_for_log_parsing = options.get('branchPath', '') # H3 full path
                    if current_full_branch_path_for_log_parsing:
                        vcs_context_basename = os.path.basename(current_full_branch_path_for_log_parsing)
                        add_output_line_to_job(job_id, f"VCS Context was empty, derived from branchPath ('{current_full_branch_path_for_log_parsing}') as: '{vcs_context_basename}' for log path construction.")
                    else:
                        add_output_line_to_job(job_id, "Warning: VCS Context is empty and branchPath is also empty for log path construction. Paths may be incorrect.")

                if final_msim_dir_option: 
                    # This is the primary case: msim ran with an explicit -dir (user-specified or derived)
                    actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename, "sim")
                    base_log_path_for_html = os.path.join("work", final_msim_dir_option, vcs_context_basename)
                    add_output_line_to_job(job_id, f"Log parsing paths based on effective msim -dir: '{final_msim_dir_option}', VCS Context: '{vcs_context_basename}'.")
                else:
                    # Fallback: msim ran without -dir (e.g., derivation failed AND user didn't provide one)
                    add_output_line_to_job(job_id, "Info: MSIM ran without an explicit -dir. Attempting log path construction based on full branch suffix.")
                    current_full_branch_path_for_log_parsing = options.get('branchPath', '') # H3 full path
                    extracted_branch_suffix = ""
                    if "work/" in current_full_branch_path_for_log_parsing:
                        # This suffix is like "msim_report_v3p0_2/mtu-vcs"
                        extracted_branch_suffix = current_full_branch_path_for_log_parsing.split("work/", 1)[1] 
                    
                    if not extracted_branch_suffix:
                        add_output_line_to_job(job_id, f"Error: Cannot determine log paths. 'work/' not in branchPath ('{current_full_branch_path_for_log_parsing}') and msim had no -dir option.")
                        log_path_error = True
                    else:
                        # Path construction: $PRJ_ICDIR/work/{extracted_branch_suffix}/sim/
                        # HTML base: work/{extracted_branch_suffix}
                        actual_sim_root_for_parsing = os.path.join(proj_root_dir_for_logs, "work", extracted_branch_suffix, "sim")
                        base_log_path_for_html = os.path.join("work", extracted_branch_suffix) 
                        add_output_line_to_job(job_id, f"Log parsing paths based on branch suffix (msim had no explicit -dir): '{base_log_path_for_html}'.")

            if not log_path_error:
                add_output_line_to_job(job_id, f"  Final calculated absolute sim root for parsing: {actual_sim_root_for_parsing}")
                add_output_line_to_job(job_id, f"  Final calculated base relative path for HTML logs: {base_log_path_for_html}")

            if log_path_error or not actual_sim_root_for_parsing or not os.path.isdir(actual_sim_root_for_parsing):
                 warning_msg = f"Rerun sim root directory for parsing ('{actual_sim_root_for_parsing}') is not valid or not found."
                 if log_path_error and not actual_sim_root_for_parsing : warning_msg = "Critical error in path calculation prevented log search."
                 add_output_line_to_job(job_id, f"Warning: {warning_msg} Detailed status update will be limited to MSIM stdout.")
                 detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout, options.get('selectedCases', []),
                                                                    None, None, job_id) # Pass None for paths
            else:
                detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout, options.get('selectedCases', []),
                                                                    actual_sim_root_for_parsing,
                                                                    base_log_path_for_html,
                                                                    job_id)
            JOB_STATUS[job_id]['detailed_test_results'] = detailed_results
            add_output_line_to_job(job_id, f"Final detailed test results: {detailed_results}")

            # Attempt to find a primary log for the whole rerun, if paths are valid
            if not log_path_error and proj_root_dir_for_logs and final_msim_dir_option and vcs_context_basename:
                rerun_output_base_abs = os.path.join(proj_root_dir_for_logs, "work", final_msim_dir_option, vcs_context_basename)
                add_output_line_to_job(job_id, f"Searching for primary msim log (run.log/comp.log) in: {rerun_output_base_abs}")
                primary_log_file = find_primary_log_for_rerun(rerun_output_base_abs)
                if primary_log_file:
                    add_output_line_to_job(job_id, f"Primary msim log file found: {primary_log_file}")
                else:
                    add_output_line_to_job(job_id, f"Warning: Primary msim log file (run.log/comp.log) not found in {rerun_output_base_abs}")
            elif not log_path_error and proj_root_dir_for_logs and base_log_path_for_html and not final_msim_dir_option : # Fallback case using extracted_branch_suffix
                # base_log_path_for_html would be "work/extracted_branch_suffix"
                rerun_output_base_abs_fallback = os.path.join(proj_root_dir_for_logs, base_log_path_for_html)
                add_output_line_to_job(job_id, f"Searching for primary msim log (run.log/comp.log) in fallback path: {rerun_output_base_abs_fallback}")
                primary_log_file_fallback = find_primary_log_for_rerun(rerun_output_base_abs_fallback)
                if primary_log_file_fallback:
                    add_output_line_to_job(job_id, f"Primary msim log file found (fallback): {primary_log_file_fallback}")
                else:
                    add_output_line_to_job(job_id, f"Warning: Primary msim log file (run.log/comp.log) not found in fallback path {rerun_output_base_abs_fallback}")


        except FileNotFoundError: # This would be if tcsh is not found, or msim/icenv within the shell
            update_job_status(job_id, "failed", "Shell (tcsh) or core command (msim/icenv) not found. Ensure tcsh, icenv, and msim are accessible.") # Updated message
            add_output_line_to_job(job_id, "Error: Shell (tcsh) or essential command (msim/icenv) not found. Check PATH and icenv setup.") # Updated message
        except Exception as e:
            update_job_status(job_id, "failed", f"An error occurred during shell (msim with icenv) execution: {e}") # Updated message
            add_output_line_to_job(job_id, f"Error during shell (msim with icenv) execution: {str(e)}") # Updated message
    except Exception as e: 
        current_app_logger.error(f"CRITICAL ERROR IN TASK {job_id}: {str(e)}", exc_info=True) 
        update_job_status(job_id, "failed", f"Critical error in task: {str(e)}")
        add_output_line_to_job(job_id, f"CRITICAL_TASK_ERROR: {str(e)}")
    finally:
        current_app_logger.info(f"--- Task ended for job_id: {job_id} ---") 

@bp.route('/rerun/<repo_id>', methods=['POST'])
def rerun_cases(repo_id): # repo_id is from URL
    # Assuming current_app is available if running in Flask context, or using bp.logger as fallback
    logger = getattr(bp, 'logger', None) # Try to get logger from blueprint
    if hasattr(request, 'getBluePrintAppLogger'): # Check if a custom method to get app logger is set
        logger = request.getBluePrintAppLogger()
    elif Flask.current_app:
        logger = Flask.current_app.logger
    
    if not logger: # Fallback to print if no logger found
        print("Warning: No Flask logger found for rerun_cases. Using print.")
        class PrintLogger:
            def info(self, msg): print(f"INFO: {msg}")
            def warning(self, msg): print(f"WARN: {msg}")
            def error(self, msg, exc_info=False): print(f"ERROR: {msg}")
        logger = PrintLogger()

    logger.info(f"--- /rerun endpoint hit for repo_id: {repo_id} ---")
    try:
        data = request.get_json()
        logger.info(f"Request data: {data}")
        if not data or 'selectedCases' not in data:
            logger.warning("Bad request to /rerun: No selectedCases provided.")
            return jsonify({"status": "error", "message": "No selectedCases provided"}), 400
        job_id = str(uuid.uuid4())
        logger.info(f"Generated job_id: {job_id} for /rerun request.")
        JOB_STATUS[job_id] = {"status": "queued", "message": "Rerun job queued.", "output_lines": []}
        # Pass the determined logger to the thread
        thread = threading.Thread(target=long_running_rerun_task, args=(job_id, data, logger))
        thread.start()
        logger.info(f"Worker thread started for job_id: {job_id}")
        return jsonify({"status": "queued", "message": "Rerun job initiated.", "job_id": job_id})
    except Exception as e:
        logger.error(f"Exception in /rerun endpoint: {e}", exc_info=True)
        error_job_id = locals().get('job_id', str(uuid.uuid4()) + "_error")
        # Update JOB_STATUS for the error
        if error_job_id not in JOB_STATUS and 'job_id' in locals():
             JOB_STATUS[error_job_id] = {"status": "failed", "message": f"Server error in /rerun: {e}", "output_lines": []}
        elif 'job_id' in locals(): # job_id was defined before exception
             update_job_status(error_job_id, "failed", f"Server error in /rerun: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {e}", "job_id": error_job_id if 'job_id' in locals() else None }), 500

@bp.route('/rerun_status/<job_id>', methods=['GET'])
def get_rerun_status_route(job_id): # Renamed to avoid conflict with function get_job_status
    status_info = get_job_status(job_id) # Calls the utility function
    return jsonify(status_info)

@bp.route('/<repo_id>')
def index(repo_id):
    if not Repo or not db:
        return "Database support is not configured. Cannot serve dynamic reports.", 500
    repo = Repo.query.get_or_404(repo_id)
    # Assuming repo.result is a dict and 'html_rpt' key exists and is an absolute path
    html_rpt_abs_path = repo.result.get('html_rpt')
    if not html_rpt_abs_path or not os.path.isabs(html_rpt_abs_path):
        return f"HTML report path not found or invalid for repo {repo_id}.", 404
    if not os.path.exists(html_rpt_abs_path):
        return f"HTML report file does not exist at {html_rpt_abs_path}", 404
        
    directory = os.path.dirname(html_rpt_abs_path)
    filename = os.path.basename(html_rpt_abs_path)
    return send_from_directory(directory, filename)

# This part is for running this blueprint as a standalone Flask app
if __name__ == '__main__':
    app = Flask(__name__)
    
    # A way to pass the app's logger to the blueprint context if needed by threads
    # This is a bit of a workaround for threads not having direct access to current_app.logger
    @bp.before_request
    def before_request_func():
        if not hasattr(request, 'getBluePrintAppLogger'):
             request.getBluePrintAppLogger = lambda: app.logger

    app.register_blueprint(bp) # Register blueprint without a URL prefix
    CORS(app) # Apply CORS to the main app for standalone running

    print("Server starting (merged version). Ensure PRJ_ICDIR environment variable is set for reruns.")
    print(f"Script directory (location of this .py file): {script_dir}") # AI_HTML
    print(f"Blueprint template folder set to: {base_dir_for_templates}")
    print("If database features are used, ensure 'models' and 'extensions' are importable and DB is configured.")
    print("The server will attempt to start on port 5000.")
    print("Expected running address: http://127.0.0.1:5000/<repo_id> (for index) or http://127.0.0.1:5000/rerun/<repo_id> (for rerun)")
    
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
