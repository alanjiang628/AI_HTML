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
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

script_dir = os.path.dirname(os.path.abspath(__file__))
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
        
        # Default HTML log path (fallback if specific case variant dir or log isn't found)
        # Uses the original case_id and 'latest'. Ensure forward slashes for HTML.
        safe_base_html_path = base_log_path_for_html.replace(os.sep, '/') if base_log_path_for_html else "unknown_html_base"
        html_log_path = f"{safe_base_html_path}/sim/{case_id}/latest/run.log"

        case_id_variant_dir_name = None
        individual_test_sim_dir_actual = None # This will be .../sim/<case_id_variant>/

        if actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
            try:
                found_match = False
                # Sort to get a predictable order if multiple startswith matches (e.g. case, case.0, case.1)
                # Prefer shorter matches if that's a convention, or specific ones like case_id itself first.
                # For now, simple startswith, first found.
                dir_items = sorted(os.listdir(actual_sim_root_for_parsing)) # Sort for consistency
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
                        else:
                            latest_log_dir = None
                            if job_id_for_logging:
                                add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): No timestamped subdirectories found.")
                    else:
                        latest_log_dir = None
                        if job_id_for_logging:
                            add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): No subdirectories found to determine newest log dir.")
                
                if latest_log_dir and os.path.isdir(latest_log_dir):
                    parse_run_log_path = os.path.join(latest_log_dir, 'parse_run.log')
                    # actual_run_log_path = os.path.join(latest_log_dir, 'run.log') # For reference
                    
                    timestamp_or_latest_name = os.path.basename(latest_log_dir)
                    # Use case_id_variant_dir_name for the HTML path. Ensure forward slashes.
                    html_log_path = f"{safe_base_html_path}/sim/{case_id_variant_dir_name.replace(os.sep, '/')}/{timestamp_or_latest_name.replace(os.sep, '/')}/run.log"
                    
                    if job_id_for_logging:
                        add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name}): Checking parse_run.log at {parse_run_log_path}")

                    status_from_parse_log = _parse_individual_parse_run_log(parse_run_log_path)
                    if status_from_parse_log:
                        current_status = status_from_parse_log
                        error_hint = "Failed (from parse_run.log)" if current_status == "FAILED" else \
                                     ("" if current_status == "PASSED" else "Status unclear from parse_run.log")
                        if job_id_for_logging:
                            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from parse_run.log: {current_status}")
                elif job_id_for_logging:
                    add_output_line_to_job(job_id_for_logging, f"For {case_id} (in {case_id_variant_dir_name if case_id_variant_dir_name else 'N/A'}): 'latest' or timestamped log directory not resolved or not a directory: {latest_log_dir}")
            elif job_id_for_logging and actual_sim_root_for_parsing and os.path.isdir(actual_sim_root_for_parsing):
                 # This case means individual_test_sim_dir_actual was not found/set
                 add_output_line_to_job(job_id_for_logging, f"For {case_id}: Could not find or access specific simulation directory for this case variant.")
        
        elif job_id_for_logging: # actual_sim_root_for_parsing is None or not a dir
            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Main sim root for parsing ('{actual_sim_root_for_parsing}') is not valid. Skipping individual log check.")


        if current_status == "UNKNOWN":
            if job_id_for_logging:
                add_output_line_to_job(job_id_for_logging, f"For {case_id}: parse_run.log status is UNKNOWN or file not found. Checking msim_stdout.")
            # Regex for specific test_id (case_id format is test_base_name_seedSEED)
            # The [TEST_DONE] log uses the full name including _seedSEED
            uvm_test_done_pattern_specific = re.compile(r"\[TEST_DONE\]\s*Test\s*" + re.escape(case_id) + r"\s*\((\w+)\)")
            for line in msim_stdout.splitlines():
                match = uvm_test_done_pattern_specific.search(line)
                if match:
                    status_from_stdout = match.group(1).upper()
                    current_status = status_from_stdout # Override if found
                    if status_from_stdout == "FAILED":
                        error_hint = "Failed (from [TEST_DONE] in msim stdout)"
                    elif status_from_stdout == "PASSED":
                        error_hint = "" # Clear hint
                    if job_id_for_logging:
                        add_output_line_to_job(job_id_for_logging, f"For {case_id}: Status from msim_stdout [TEST_DONE]: {current_status}")
                    break 
        
        results_map[case_id] = {
            "id": case_id,
            "status": current_status,
            "error_hint": error_hint,
            "new_log_path": html_log_path 
        }
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"For {case_id}: Final determined status: {current_status}, Log: {html_log_path}")

    return list(results_map.values())


def prepare_rerun_hjson_files(options, temp_rerun_dir, ip_name):
    print(f"--- prepare_rerun_hjson_files called for IP: {ip_name} ---")

    job_id_for_logging = options.get("job_id_for_logging") 

    proj_root_dir = os.environ.get('PRJ_ICDIR')
    if not proj_root_dir:
        error_msg = "CRITICAL ERROR: Environment variable PRJ_ICDIR is not set. Cannot locate original HJSON files."
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, "Error: PRJ_ICDIR environment variable not set. Configure server environment.")
        return None
    
    print(f"Using PRJ_ICDIR from environment: {proj_root_dir}")

    original_hjson_filename = f"{ip_name}.hjson"
    original_hjson_path = os.path.join(
        proj_root_dir, 
        "dv", 
        "sim_ctrl", 
        "ts", 
        original_hjson_filename
    )
    print(f"Calculated original HJSON path: {original_hjson_path}")
    
    target_hjson_dir = os.path.join(proj_root_dir, "dv", "sim_ctrl", "ts", "temp")
    
    print(f"Target directory for 'rerun.hjson' under PRJ_ICDIR: {target_hjson_dir}")

    try:
        os.makedirs(target_hjson_dir, exist_ok=True) 
        print(f"Ensured target directory for 'rerun.hjson' exists: {target_hjson_dir}")
    except Exception as e:
        error_msg = f"CRITICAL ERROR: Failed to create target directory {target_hjson_dir} for 'rerun.hjson': {e}"
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: Failed to create target directory {target_hjson_dir}: {e}")
        return None

    temp_target_hjson_path = os.path.join(target_hjson_dir, "rerun.hjson")
    print(f"Temporary target HJSON path for copy: {temp_target_hjson_path}")

    if not os.path.exists(original_hjson_path):
        error_msg = f"CRITICAL ERROR: Source HJSON file does not exist at the calculated path: {original_hjson_path}"
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: Source HJSON not found: {original_hjson_path}")
        return None
    
    print(f"Source HJSON file found at {original_hjson_path}")

    try:
        shutil.copy(original_hjson_path, temp_target_hjson_path)
        print(f"Successfully copied {original_hjson_path} to {temp_target_hjson_path}")
    except Exception as e:
        error_msg = f"Error: Could not copy HJSON file from {original_hjson_path} to {temp_target_hjson_path}: {e}"
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: Failed to copy HJSON: {e}")
        return None

    try:
        with open(temp_target_hjson_path, 'r') as file:
            target_hjson_data = hjson.load(file) 
        print(f"Successfully loaded HJSON data from {temp_target_hjson_path}")
    except Exception as e:
        error_msg = f"Error: Could not read or parse HJSON from {temp_target_hjson_path}: {e}"
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: Failed to parse HJSON {temp_target_hjson_path}: {e}")
        return None

    final_tests_section_for_output = [] 
    test_names_for_regression_list = []
    original_tests_from_hjson = target_hjson_data.get("tests", []) 
    original_test_defs_map_by_base_name = {}

    if isinstance(original_tests_from_hjson, list):
        print(f"Info: Original 'tests' section in {temp_target_hjson_path} is a list. Parsing for templates.")
        for test_def in original_tests_from_hjson:
            if isinstance(test_def, dict) and "name" in test_def:
                original_test_defs_map_by_base_name[test_def["name"]] = test_def
            else:
                print(f"Warning: Malformed item in original 'tests' list: {test_def}. Skipping.")
    elif isinstance(original_tests_from_hjson, dict):
        print(f"Info: Original 'tests' section in {temp_target_hjson_path} is a dictionary. Parsing for templates.")
        for base_name, test_def in original_tests_from_hjson.items():
            if isinstance(test_def, dict):
                original_test_defs_map_by_base_name[base_name] = test_def
            else:
                print(f"Warning: Malformed item in original 'tests' dict for key '{base_name}': {test_def}. Skipping.")
    else:
        print(f"Warning: Original 'tests' section in {temp_target_hjson_path} is neither list nor dict (type: {type(original_tests_from_hjson)}). Cannot find base test definitions for templates.")

    selected_cases_for_this_ip = [
        case_id for case_id in options.get('selectedCases', []) 
        if case_id.startswith(ip_name + "_") 
    ]

    if not selected_cases_for_this_ip:
        print(f"Info: No cases selected for IP '{ip_name}'. 'tests' section in rerun.hjson will be empty.")
    else:
        for case_id_with_seed in selected_cases_for_this_ip:
            parts = case_id_with_seed.split("_seed")
            if len(parts) != 2:
                print(f"Warning: Could not parse base name and seed from '{case_id_with_seed}'. Skipping this case for HJSON.")
                continue
            base_test_name = parts[0]
            seed_str = parts[1]
            
            try:
                seed_val = int(seed_str)
            except ValueError:
                print(f"Warning: Invalid seed value '{seed_str}' in '{case_id_with_seed}'. Skipping this case for HJSON.")
                continue

            original_def_template = original_test_defs_map_by_base_name.get(base_test_name)
            
            new_test_def_object = {}
            if original_def_template:
                print(f"Info: Found original definition template for base test '{base_test_name}'.")
                new_test_def_object = copy.deepcopy(original_def_template)
            else:
                print(f"Warning: Original definition template for base test '{base_test_name}' not found. Creating a minimal definition for rerun.")
                new_test_def_object["uvm_test_seq"] = f"unknown_vseq_for_{base_test_name}" 
                new_test_def_object["build_mode"] = f"unknown_build_mode_for_{base_test_name}"

            new_test_def_object['name'] = case_id_with_seed 
            
            if 'seed' in new_test_def_object:
                del new_test_def_object['seed'] 

            current_run_opts = new_test_def_object.get("run_opts", [])
            if not isinstance(current_run_opts, list):
                print(f"Warning: 'run_opts' for base test '{base_test_name}' was not a list (type: {type(current_run_opts)}). Re-initializing for seed.")
                current_run_opts = []
            
            updated_run_opts = [opt for opt in current_run_opts if not str(opt).startswith("+ntb_random_seed=")]
            updated_run_opts.append(f"+ntb_random_seed={seed_val}")
            new_test_def_object['run_opts'] = updated_run_opts
            
            print(f"Info: Updated 'run_opts' for test '{case_id_with_seed}' to include '+ntb_random_seed={seed_val}'.")

            final_tests_section_for_output.append(new_test_def_object)
            test_names_for_regression_list.append(case_id_with_seed) 
            print(f"Info: Prepared test definition for '{case_id_with_seed}' (with run_opts for seed) to be included in rerun.hjson 'tests' list.")

    target_hjson_data['tests'] = final_tests_section_for_output
    print(f"Info: 'tests' section of rerun.hjson will now be a list with {len(final_tests_section_for_output)} test definition objects.")

    rerun_regression_group = {
        "name": "rerun",
        "tests": test_names_for_regression_list 
    }
    
    if not isinstance(target_hjson_data.get("regressions"), list):
        print(f"Warning: 'regressions' section in loaded HJSON is not a list (or does not exist). Initializing as a new list with the 'rerun' group.")
        target_hjson_data["regressions"] = [rerun_regression_group]
    else:
        existing_rerun_index = next((i for i, reg in enumerate(target_hjson_data["regressions"]) if isinstance(reg, dict) and reg.get("name") == "rerun"), None)
        if existing_rerun_index is not None:
            print("Info: Updating existing 'rerun' regression group in HJSON.")
            target_hjson_data["regressions"][existing_rerun_index] = rerun_regression_group
        else:
            print("Info: Adding new 'rerun' regression group to HJSON.")
            target_hjson_data["regressions"].append(rerun_regression_group)
    
    print(f"Info: Final 'rerun' regression group's 'tests' list: {test_names_for_regression_list}")

    try:
        with open(temp_target_hjson_path, 'w') as file:
            hjson.dump(target_hjson_data, file, indent=2) 
        print(f"Successfully wrote modified HJSON to {temp_target_hjson_path}")
        return temp_target_hjson_path
    except Exception as e:
        error_msg = f"Error: Could not write modified HJSON to {temp_target_hjson_path}: {e}"
        print(error_msg)
        if job_id_for_logging:
            add_output_line_to_job(job_id_for_logging, f"Error: Failed to write HJSON {temp_target_hjson_path}: {e}")
        return None

def long_running_rerun_task(job_id, options):
    app.logger.info(f"--- Starting long_running_rerun_task for job_id: {job_id} ---")
    app.logger.info(f"Options received by task: {options}")
    try:
        options["job_id_for_logging"] = job_id 
        
        num_selected_cases = len(options.get('selectedCases', []))
        JOB_STATUS[job_id]['progress_summary'] = {
            "total_selected": num_selected_cases,
            "processed_count": 0,
            "passed_count": 0,
            "failed_count": 0
        }
        update_job_status(job_id, "preparing_hjson", "Preparing HJSON files...")
        add_output_line_to_job(job_id, "Rerun task started. Preparing HJSON files...")

        temp_rerun_dir_name = f"temp_rerun_{job_id}_{str(uuid.uuid4())[:8]}"
        temp_rerun_dir = os.path.join(script_dir, temp_rerun_dir_name) 

        try:
            os.makedirs(temp_rerun_dir, exist_ok=True)
            add_output_line_to_job(job_id, f"Created temporary directory for rerun: {temp_rerun_dir}")
        except Exception as e:
            update_job_status(job_id, "failed", f"Failed to create temp directory: {e}")
            add_output_line_to_job(job_id, f"Error: Failed to create temporary directory {temp_rerun_dir}: {e}")
            return

        branch_path = options.get('branchPath')
        if not branch_path:
            update_job_status(job_id, "failed", "Branch path not provided by client.")
            add_output_line_to_job(job_id, "Error: Branch path is missing in the request. Cannot determine IP context for HJSON.")
            return

        add_output_line_to_job(job_id, f"Received branch path for IP context: {branch_path}")
        
        derived_ip_name = None
        try:
            ip_folder_name = os.path.basename(branch_path)
            derived_ip_name = ip_folder_name.split('-', 1)[0]
            if not derived_ip_name: 
                raise ValueError("Derived IP name is empty.")
        except Exception as e:
            update_job_status(job_id, "failed", f"Failed to derive IP name from branch path: {branch_path}. Error: {e}")
            add_output_line_to_job(job_id, f"Error: Could not derive IP name from branch path '{branch_path}': {e}")
            return

        add_output_line_to_job(job_id, f"Derived IP name for HJSON context: {derived_ip_name}")

        ip_names_to_process = {derived_ip_name} 

        generated_hjson_paths_map = {} 
        all_hjson_prepared_successfully = True

        for ip_name in ip_names_to_process: 
            add_output_line_to_job(job_id, f"Processing IP: {ip_name} for HJSON preparation.")
            hjson_path = prepare_rerun_hjson_files(options, temp_rerun_dir, ip_name) 
            
            if hjson_path:
                generated_hjson_paths_map[ip_name] = hjson_path
                add_output_line_to_job(job_id, f"Successfully prepared HJSON for {ip_name} at {hjson_path}")
            else:
                add_output_line_to_job(job_id, f"Error: Failed to prepare HJSON for IP: {ip_name}. See server console / job log for details.")
                update_job_status(job_id, "failed", f"Failed to prepare HJSON for {ip_name}.")
                all_hjson_prepared_successfully = False
                break 

        if not all_hjson_prepared_successfully:
            add_output_line_to_job(job_id, "HJSON preparation failed for one or more IPs. Aborting msim launch.")
            return

        if not generated_hjson_paths_map:
            update_job_status(job_id, "failed", "No HJSON files were generated.")
            add_output_line_to_job(job_id, "Error: No HJSON files were successfully generated. Cannot proceed with msim.")
            return

        update_job_status(job_id, "hjson_prepared", "HJSON files prepared. Starting MSIM...")
        add_output_line_to_job(job_id, "All HJSON files prepared successfully.")
        
        if not generated_hjson_paths_map: 
            add_output_line_to_job(job_id, "Error: HJSON preparation step did not yield a file path. Cannot construct msim command.")
            update_job_status(job_id, "failed", "MSIM command construction failed: HJSON preparation error.")
            return

        prepared_hjson_actual_path = list(generated_hjson_paths_map.values())[0]
        add_output_line_to_job(job_id, f"MSIM will be invoked with 'rerun' as config argument.")
        add_output_line_to_job(job_id, f"This expects msim to use the HJSON file prepared at: {prepared_hjson_actual_path}")

        msim_command_parts = ["msim", "rerun", "-t", "rerun"] 
        add_output_line_to_job(job_id, f"Base msim command: msim rerun -t rerun")
        
        if not options.get('rebuildCases', False): 
            msim_command_parts.append("-so")
            add_output_line_to_job(job_id, "Adding -so (not rebuilding all selected cases / skip optimize)")
        else:
            add_output_line_to_job(job_id, "Rebuilding all selected cases (no -so flag)")

        if options.get('includeWaveform'):
            msim_command_parts.append("-w")
            add_output_line_to_job(job_id, "Adding -w (include waveform)")

        if options.get('openCoverage'): 
            msim_command_parts.append("-c")
            add_output_line_to_job(job_id, "Adding -c (open coverage)")

        sim_time_hours_str = options.get('simTimeHours', "0") 
        try:
            sim_time_hours = int(sim_time_hours_str)
            if sim_time_hours > 0:
                sim_time_minutes = sim_time_hours * 60
                msim_command_parts.extend(["-rto", str(sim_time_minutes)])
                add_output_line_to_job(job_id, f"Adding -rto {sim_time_minutes} (simulation time override)")
        except ValueError:
            add_output_line_to_job(job_id, f"Warning: Invalid value for simTimeHours: {sim_time_hours_str}. Not adding -rto.")

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
                # The directory for -dir is the one immediately after "work/"
                derived_dir_for_msim = os.path.normpath(path_after_work).split(os.sep)[0]
                if derived_dir_for_msim and derived_dir_for_msim != '.' and derived_dir_for_msim != os.path.basename(path_after_work): # Ensure it's a directory, not the vcs-context itself
                    final_msim_dir_option = derived_dir_for_msim
                    add_output_line_to_job(job_id, f"Derived msim -dir from branchPath ('{full_branch_path_for_msim_dir_derivation}') as: '{final_msim_dir_option}'.")
                else:
                    add_output_line_to_job(job_id, f"Warning: Could not derive a valid directory for msim -dir from branchPath '{full_branch_path_for_msim_dir_derivation}' (path after work: '{path_after_work}', derived: '{derived_dir_for_msim}'). MSIM may run without an explicit -dir if this was the only source.")
            else:
                add_output_line_to_job(job_id, f"Warning: 'work/' not found in branchPath '{full_branch_path_for_msim_dir_derivation}'. Cannot derive implicit -dir for msim.")

        if final_msim_dir_option:
            msim_command_parts.extend(["-dir", final_msim_dir_option])
            # Log message already added when final_msim_dir_option was determined
        else:
            add_output_line_to_job(job_id, "MSIM command will not include an explicit -dir option (neither user-specified nor successfully derived).")
        
        elab_opts_value = options.get('elabOpts', '').strip() 
        if elab_opts_value:
            msim_command_parts.extend(["-elab", elab_opts_value])
            add_output_line_to_job(job_id, f"Adding -elab \"{elab_opts_value}\"")

        vlogan_opts_value = options.get('vloganOpts', '').strip() 
        if vlogan_opts_value:
            msim_command_parts.extend(["-vlog", vlogan_opts_value])
            add_output_line_to_job(job_id, f"Adding -vlog \"{vlogan_opts_value}\"")

        run_opts_value = options.get('runOpts', '').strip() 
        if run_opts_value:
            msim_command_parts.extend(["-ro", run_opts_value])
            add_output_line_to_job(job_id, f"Adding -ro \"{run_opts_value}\"")

        msim_full_command = " ".join(msim_command_parts) 
        add_output_line_to_job(job_id, f"Constructed msim command: {msim_full_command}")
        update_job_status(job_id, "running_msim", "Executing MSIM command...", command=msim_full_command)
        add_output_line_to_job(job_id, "Executing MSIM. This may take some time...")
        
        try:
            process = subprocess.Popen(msim_command_parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
            
            if process.stdout:
                for line in iter(process.stdout.readline, ''):
                    print(line, end='') 
                    add_output_line_to_job(job_id, line.strip())
                process.stdout.close()

            return_code = process.wait()
            
            stderr_output = ""
            if process.stderr:
                stderr_output = process.stderr.read()
                process.stderr.close()
                if stderr_output:
                    add_output_line_to_job(job_id, "MSIM Stderr:")
                    for line_err in stderr_output.splitlines(): 
                        add_output_line_to_job(job_id, line_err.strip())
            
            final_status_key = "completed" if return_code == 0 else "failed"
            final_status_message = "MSIM run completed successfully." if return_code == 0 else f"MSIM run failed with return code {return_code}."
            
            update_job_status(job_id, final_status_key, final_status_message, returncode=return_code, stderr=stderr_output if return_code != 0 else None)
            add_output_line_to_job(job_id, final_status_message)

            full_msim_stdout = "\n".join(JOB_STATUS[job_id].get("output_lines", [])) 
            
            rerun_base_dir_for_logs = None
            dir_option_value = options.get('dirOption', '').strip()
            if dir_option_value:
                if os.path.isabs(dir_option_value):
                    rerun_base_dir_for_logs = dir_option_value
                else:
                    rerun_base_dir_for_logs = os.path.abspath(os.path.join(os.getcwd(), dir_option_value))
                add_output_line_to_job(job_id, f"MSIM -dir option was: '{dir_option_value}'. Absolute search path for logs: {rerun_base_dir_for_logs}")
            else:
                rerun_base_dir_for_logs = os.getcwd() 
                add_output_line_to_job(job_id, f"MSIM -dir option not specified. Searching for logs within server CWD: {rerun_base_dir_for_logs}")

            # Determine paths for log parsing and HTML links
            # This uses `final_msim_dir_option` which was determined before msim command construction
            # and `vcs_context` from client options.
            proj_root_dir = os.environ.get('PRJ_ICDIR')
            # `final_msim_dir_option` is the one ACTUALLY passed to `msim -dir` (user-spec or derived)
            # `full_branch_path` is from options.get('branchPath', '')
            # `vcs_context_from_client` is from options.get('vcsContext', '')

            actual_sim_root_for_parsing = None 
            base_log_path_for_html = None      
            log_path_error = False

            if not proj_root_dir:
                add_output_line_to_job(job_id, "CRITICAL Error: PRJ_ICDIR environment variable is not set. Cannot determine log parsing paths.")
                log_path_error = True
            else:
                # Determine vcs_context_basename (e.g., "mtu-vcs")
                vcs_context_from_client = options.get('vcsContext', '')
                vcs_context_basename = os.path.basename(vcs_context_from_client) if vcs_context_from_client else ""
                
                if not vcs_context_basename:
                    # Fallback: try to derive from full_branch_path if vcsContext option was empty
                    current_full_branch_path = options.get('branchPath', '')
                    if current_full_branch_path:
                        vcs_context_basename = os.path.basename(current_full_branch_path)
                        add_output_line_to_job(job_id, f"VCS Context was empty, derived from branchPath ('{current_full_branch_path}') as: '{vcs_context_basename}' for log path construction.")
                    else:
                        add_output_line_to_job(job_id, "Warning: VCS Context is empty and branchPath is also empty. Log paths might be incomplete or incorrect.")
                        # log_path_error = True # Decide if this is critical enough to stop log parsing

                if final_msim_dir_option: 
                    # This is the primary case: msim ran with an explicit -dir (user-specified or derived)
                    actual_sim_root_for_parsing = os.path.join(proj_root_dir, "work", final_msim_dir_option, vcs_context_basename, "sim")
                    base_log_path_for_html = os.path.join("work", final_msim_dir_option, vcs_context_basename) # Relative path for HTML
                    add_output_line_to_job(job_id, f"Log parsing paths based on effective msim -dir: '{final_msim_dir_option}', VCS Context: '{vcs_context_basename}'.")
                else:
                    # Fallback: msim ran without -dir (e.g., derivation failed AND user didn't provide one)
                    add_output_line_to_job(job_id, "Info: MSIM ran without an explicit -dir. Attempting log path construction based on full branch suffix.")
                    current_full_branch_path = options.get('branchPath', '') # Re-fetch for clarity in this block
                    extracted_branch_suffix = ""
                    if "work/" in current_full_branch_path:
                        # This suffix is like "msim_report_v3p0_2/mtu-vcs"
                        extracted_branch_suffix = current_full_branch_path.split("work/", 1)[1] 
                    
                    if not extracted_branch_suffix:
                        add_output_line_to_job(job_id, f"Error: Cannot determine log paths. 'work/' not in branchPath ('{current_full_branch_path}') and msim had no -dir option.")
                        log_path_error = True
                    else:
                        # Path construction: $PRJ_ICDIR/work/{extracted_branch_suffix}/sim/
                        # HTML base: work/{extracted_branch_suffix}
                        actual_sim_root_for_parsing = os.path.join(proj_root_dir, "work", extracted_branch_suffix, "sim")
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
                # Pass the calculated actual_sim_root_for_parsing and base_log_path_for_html
                detailed_results = parse_msim_output_for_test_statuses(full_msim_stdout, options.get('selectedCases', []),
                                                                    actual_sim_root_for_parsing,
                                                                    base_log_path_for_html,
                                                                    job_id)
            
            JOB_STATUS[job_id]['detailed_test_results'] = detailed_results
            add_output_line_to_job(job_id, f"Final detailed test results: {detailed_results}")

        except FileNotFoundError:
            update_job_status(job_id, "failed", "MSIM command not found. Ensure msim is in PATH.")
            add_output_line_to_job(job_id, "Error: MSIM command not found. Please check system PATH or msim setup.")
        except Exception as e:
            update_job_status(job_id, "failed", f"An error occurred during MSIM execution: {e}")
            add_output_line_to_job(job_id, f"Error during MSIM execution: {str(e)}")
        finally:
            pass
    except Exception as e: 
        app.logger.error(f"CRITICAL ERROR IN TASK {job_id}: {str(e)}", exc_info=True) 
        update_job_status(job_id, "failed", f"Critical error in task: {str(e)}")
        add_output_line_to_job(job_id, f"CRITICAL_TASK_ERROR: {str(e)}")
    finally:
        app.logger.info(f"--- Task ended for job_id: {job_id} ---") 

@app.route('/rerun', methods=['POST'])
def rerun_cases():
    app.logger.info(f"--- /rerun endpoint hit ---")
    try:
        data = request.get_json()
        app.logger.info(f"Request data: {data}")

        if not data or 'selectedCases' not in data:
            app.logger.warning("Bad request to /rerun: No selectedCases provided.")
            return jsonify({"status": "error", "message": "No selectedCases provided"}), 400

        job_id = str(uuid.uuid4())
        app.logger.info(f"Generated job_id: {job_id} for /rerun request.")
        JOB_STATUS[job_id] = {"status": "queued", "message": "Rerun job queued.", "output_lines": []}
        
        thread = threading.Thread(target=long_running_rerun_task, args=(job_id, data))
        thread.start()
        app.logger.info(f"Worker thread started for job_id: {job_id}")
        
        return jsonify({"status": "queued", "message": "Rerun job initiated.", "job_id": job_id})
    except Exception as e:
        app.logger.error(f"Exception in /rerun endpoint: {e}", exc_info=True)
        error_job_id = locals().get('job_id', str(uuid.uuid4()) + "_error")
        if error_job_id not in JOB_STATUS and 'job_id' in locals():
             JOB_STATUS[error_job_id] = {"status": "failed", "message": f"Server error in /rerun: {e}", "output_lines": []}
        elif 'job_id' in locals():
             update_job_status(error_job_id, "failed", f"Server error in /rerun: {e}")

        return jsonify({"status": "error", "message": f"Internal server error: {e}", "job_id": error_job_id if 'job_id' in locals() else None }), 500

@app.route('/rerun_status/<job_id>', methods=['GET'])
def get_rerun_status(job_id):
    status_info = get_job_status(job_id)
    return jsonify(status_info)

@app.route('/')
def index():
    return send_from_directory(script_dir, 'interactive_live_report.html')


if __name__ == '__main__':
    print("Server starting. Ensure PRJ_ICDIR environment variable is set if rerunning HJSON-based tests.")
    print(f"Script directory: {script_dir}")
    print("HTML report will be served from this directory: interactive_live_report.html")
    print("The server will attempt to start on a fixed port (5000).")
    print("If port 5000 is unavailable, the server will fail to start.")
    print("Expected running address: http://127.0.0.1:5000/")
    
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
