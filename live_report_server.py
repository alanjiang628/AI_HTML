import os
import shutil
import subprocess
import threading # To run msim in a non-blocking way from Flask
import hjson # Make sure hjson is installed (pip install hjson)
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS # For handling Cross-Origin Resource Sharing (pip install Flask-CORS)

app = Flask(__name__)
CORS(app) # Enable CORS for all routes, helpful if HTML is not served by Flask

# --- Configuration (Adjust as needed) ---
# Assuming PRJ_ICDIR is set in the environment where this server runs
PRJ_ICDIR = os.environ.get('PRJ_ICDIR')
if not PRJ_ICDIR:
    print("ERROR: The PRJ_ICDIR environment variable is not set. This script cannot run without it.")
    # For a real app, you might raise an exception or exit, 
    # but for now, we'll let it proceed and fail later if PRJ_ICDIR is used.

# Base directory for reports, similar to msum.py
HOME_DIRECTORY = os.path.expanduser('~')
REPORT_OUTPUT_DIRECTORY = os.path.join(HOME_DIRECTORY, 'regression_report_temp_interactive')

# --- Helper Functions (Adapted from/inspired by msum.py logic) ---

def get_hjson_paths(base_case_name, temp_dir_path):
    """
    Determines the HJSON filename and its full path within the temp directory.
    This needs to be adapted based on how your HJSON files are named and structured.
    The logic from msum.py:
    hjson_filename_prefix = vcs_dir.replace('-vcs', '') # This implies vcs_dir is known
    hjson_filename = hjson_filename_prefix + '.hjson'
    full_hjson_path = os.path.join(temp_dir_path, hjson_filename)
    """
    # This is a placeholder. We need a robust way to map a base_case_name
    # (or perhaps the original vcs_dir if we can get it from the HTML)
    # to its corresponding HJSON file.
    # For now, let's assume a convention or that this info is passed.
    # This part is CRITICAL and needs to match your project's structure.
    
    # Simplistic assumption: all selected cases might belong to one primary hjson or
    # we need to group them by their original hjson.
    # The `msum.py` grouped selected cases by `full_hjson_path`.
    # The HTML currently sends a flat list of `selectedCases`.
    # We need to determine which HJSON file each selected case belongs to.
    # This might require more info from the HTML or a lookup mechanism.

    # For this initial version, let's assume all reruns go into a generic 'rerun_group.hjson'
    # or that the HTML needs to provide more context for each case.
    # Let's assume for now we operate on a primary hjson file for the rerun group.
    # This is a major simplification and likely needs refinement.
    
    # A more robust approach would be to pass the original vcs_dir for each case from the HTML,
    # or have a mapping.
    # For now, let's assume a single 'default_rerun_config.hjson' or similar.
    # This part needs to be carefully adapted from msum.py's logic for grouping.
    
    # Let's assume for now that the 'dirOption' from the console might give a hint
    # to the vcs_dir, e.g., if dirOption is 'mtu-vcs', then hjson is 'mtu.hjson'.
    # This is still an assumption.
    
    # Fallback: use a generic name if no specific logic is implemented yet.
    # This is a placeholder and needs to be replaced with actual logic from msum.py
    # on how it determines the hjson_filename based on vcs_dir.
    # For now, we'll just create a dummy hjson name.
    # This part is highly dependent on your project structure and how msum.py derived hjson_filename.
    # We need to replicate the logic:
    # hjson_filename_prefix = vcs_dir.replace('-vcs', '')
    # hjson_filename = hjson_filename_prefix + '.hjson'
    # For now, let's assume a default or that this needs to be passed/derived.
    # This function is no longer needed as its logic is integrated into prepare_rerun_hjson_files
    pass


def prepare_rerun_hjson_files(options, temp_rerun_dir):
    """
    Prepares HJSON files in a temporary directory for the rerun.
    This function replicates the HJSON manipulation logic from msum.py.
    - Copies all.hjson and its dependencies into temp_rerun_dir.
    - Modifies the relevant test HJSON files (also copied to temp_rerun_dir) to include a 'rerun' group.
    """
    if not PRJ_ICDIR:
        raise ValueError("PRJ_ICDIR is not set. Cannot prepare HJSON files.")

    ts_dir_path = os.path.join(PRJ_ICDIR, 'dv', 'sim_ctrl', 'ts') # Base for original HJSONs

    # 1. Process all.hjson and its 'use_cfgs'
    original_all_hjson_path = os.path.join(ts_dir_path, 'all.hjson')
    temp_all_hjson_path = os.path.join(temp_rerun_dir, 'all.hjson')

    if not os.path.exists(original_all_hjson_path):
        raise FileNotFoundError(f"Original all.hjson not found at {original_all_hjson_path}")

    with open(original_all_hjson_path, 'r') as file:
        all_hjson_data = hjson.load(file)
    
    original_use_cfgs = all_hjson_data.get('use_cfgs', [])
    temp_use_cfgs = []

    for hjson_relative_path_from_proj_root in original_use_cfgs:
        # Path like '{proj_root}/dv/sim_ctrl/ts/common/common.hjson'
        path_part = hjson_relative_path_from_proj_root.replace('{proj_root}/', '')
        original_cfg_path = os.path.join(PRJ_ICDIR, path_part)

        # Destination path in temp_rerun_dir, maintaining relative structure from ts_dir_path
        relative_to_ts_dir = os.path.relpath(original_cfg_path, ts_dir_path)
        temp_cfg_path = os.path.join(temp_rerun_dir, relative_to_ts_dir)
        
        temp_cfg_dir = os.path.dirname(temp_cfg_path)
        if not os.path.exists(temp_cfg_dir):
            os.makedirs(temp_cfg_dir, exist_ok=True)
        
        if not os.path.exists(original_cfg_path):
            raise FileNotFoundError(f"use_cfgs file not found: {original_cfg_path}")
        shutil.copyfile(original_cfg_path, temp_cfg_path)
        
        # Path for use_cfgs in temp all.hjson should be relative to PRJ_ICDIR
        # but pointing to the file within temp_rerun_dir (which is inside ts_dir_path/temp_reruns/)
        # The structure inside temp_rerun_dir mirrors the structure from ts_dir_path downwards.
        # So, the {proj_root}/dv/sim_ctrl/ts/ part remains, and the rest points into temp_rerun_dir.
        # Example: original {proj_root}/dv/sim_ctrl/ts/common/common.hjson
        # becomes {proj_root}/dv/sim_ctrl/ts/temp_reruns/current_rerun_hjsons/common/common.hjson
        # This requires careful path construction.
        # The path stored in use_cfgs should be resolvable by msim when CWD is dv/sim_ctrl/ts
        # and it looks for temp_reruns/current_rerun_hjsons/...
        
        # Path relative from PRJ_ICDIR to the copied file in temp_rerun_dir
        path_in_temp_from_prj_icdir = os.path.relpath(temp_cfg_path, PRJ_ICDIR).replace('\\', '/')
        temp_use_cfgs.append(f"{{proj_root}}/{path_in_temp_from_prj_icdir}")

    all_hjson_data['use_cfgs'] = temp_use_cfgs
    with open(temp_all_hjson_path, 'w') as file:
        hjson.dump(all_hjson_data, file, indent=4)
    print(f"Processed and copied all.hjson to {temp_all_hjson_path} with updated use_cfgs.")


    # 2. Modify the target HJSON file for the selected cases
    vcs_context = options.get('vcsContext') # e.g., "mtu-vcs"
    if not vcs_context:
        raise ValueError("vcsContext not provided in options, cannot determine target HJSON.")

    hjson_filename_prefix = vcs_context.replace('-vcs', '')
    target_hjson_filename = f"{hjson_filename_prefix}.hjson" # e.g., "mtu.hjson"

    # Determine original path and temp path for the target HJSON
    # msum.py logic:
    #   hjson_filename = os.path.basename(hjson_file_path) -> e.g. mtu.hjson
    #   base_name = os.path.splitext(hjson_filename)[0] -> e.g. mtu
    #   subdir_path = os.path.join(os.path.dirname(hjson_file_path), base_name) -> e.g. .../temp/mtu
    #   if os.path.isdir(subdir_path):
    #       hjson_file_path = os.path.join(subdir_path, hjson_filename) -> .../temp/mtu/mtu.hjson
    # This implies target HJSONs might be in subdirectories named after themselves (without .hjson)

    original_target_hjson_base_path = os.path.join(ts_dir_path, target_hjson_filename)
    original_target_hjson_subdir_path = os.path.join(ts_dir_path, hjson_filename_prefix, target_hjson_filename)

    if os.path.exists(original_target_hjson_subdir_path):
        original_target_hjson_path = original_target_hjson_subdir_path
        # Path in temp dir should also be in a subdir
        temp_target_hjson_path = os.path.join(temp_rerun_dir, hjson_filename_prefix, target_hjson_filename)
    elif os.path.exists(original_target_hjson_base_path):
        original_target_hjson_path = original_target_hjson_base_path
        temp_target_hjson_path = os.path.join(temp_rerun_dir, target_hjson_filename)
    else:
        raise FileNotFoundError(f"Target HJSON file {target_hjson_filename} not found in {ts_dir_path} or its '{hjson_filename_prefix}' subdirectory.")

    temp_target_hjson_dir = os.path.dirname(temp_target_hjson_path)
    if not os.path.exists(temp_target_hjson_dir):
        os.makedirs(temp_target_hjson_dir, exist_ok=True)
    
    shutil.copyfile(original_target_hjson_path, temp_target_hjson_path)
    
    with open(temp_target_hjson_path, 'r') as file:
        target_hjson_data = hjson.load(file)

    if "regressions" not in target_hjson_data:
        target_hjson_data["regressions"] = []
    
    # Ensure selectedCases are just names, not with extensions if msum.py removed them
    # msum.py: base_case_name = case.split(".")[0]
    # HTML sends case names as they appear, which might be base names already.
    # Assuming options['selectedCases'] contains base names.
    
    new_regression_group = {
        "name": "rerun",
        "tests": options['selectedCases']
    }

    existing_rerun_index = next((i for i, reg in enumerate(target_hjson_data["regressions"]) if reg.get("name") == "rerun"), None)
    if existing_rerun_index is not None:
        target_hjson_data["regressions"][existing_rerun_index] = new_regression_group
    else:
        target_hjson_data["regressions"].append(new_regression_group)

    with open(temp_target_hjson_path, 'w') as file:
        hjson.dump(target_hjson_data, file, indent=2)
    
    print(f"Prepared target HJSON for rerun: {temp_target_hjson_path}")
    return temp_all_hjson_path


def execute_msim_rerun(options, temp_rerun_dir):
    """
    Constructs and executes the msim rerun command.
    Returns a tuple (success_boolean, message_string).
    """
    if not PRJ_ICDIR:
        return False, "PRJ_ICDIR environment variable is not set."

    # Prepare HJSON files (this should ideally be more robust)
    try:
        # This function needs to correctly set up all.hjson and individual test hjsons
        # in temp_rerun_dir. The current implementation is a placeholder.
        prepare_rerun_hjson_files(options, temp_rerun_dir)
    except Exception as e:
        return False, f"Error preparing HJSON files: {str(e)}"

    # Construct msim command parts
    wave_opt = '-w' if options.get('includeWaveform') else ''
    rebuild_opt = '' if options.get('rebuildCases') else '-so' # msum: rebuild_opt = '' if do_rebuild else '-so'
    coverage_opt = '-c' if options.get('openCoverage') else ''
    
    run_time_hours = int(options.get('simTimeHours', 0))
    run_time_minutes = run_time_hours * 60
    run_time_opt = f'-rto {run_time_minutes}' if run_time_minutes > 0 else ''
    
    dir_option_value = options.get('dirOption', '').strip()
    if not dir_option_value:
        # Default directory name if not provided, e.g., based on current timestamp or a fixed name
        # msum.py uses os.path.basename(os.getcwd()) if not set.
        # For a server, os.getcwd() might not be relevant.
        # Let's use a fixed default or require it.
        dir_option_value = "rerun_output_default" 
    dir_opt = f'-dir {dir_option_value}'

    elab_opts_val = options.get('elabOpts', '').strip()
    elab_opt_str = f'-elab_opts "{elab_opts_val}"' if elab_opts_val else '' # Ensure quoting if opts have spaces

    vlogan_opts_val = options.get('vloganOpts', '').strip()
    vlogan_opt_str = f'-vlogan_opts "{vlogan_opts_val}"' if vlogan_opts_val else ''

    run_opts_val = options.get('runOpts', '').strip()
    run_opt_str = f'-run_opts "{run_opts_val}"' if run_opts_val else ''

    # The `msim` command needs to be run from a context where it can find `all.hjson`
    # (the one in `temp_rerun_dir`). So, we might need to `cd` into `temp_rerun_dir`
    # or use `msim -f temp_rerun_dir/all.hjson`.
    # `msum.py` generates `rerun_cases.sh` which implies `msim` is run from a specific context.
    # Let's assume `msim` needs to be run from `PRJ_ICDIR` or `PRJ_ICDIR/dv/sim_ctrl/ts`
    # and it will pick up `temp/all.hjson` or `temp/rerun.hjson` if `ts` is the CWD.
    # This is a critical detail.
    # For now, let's assume we run from PRJ_ICDIR and msim knows about the temp structure.
    # The `msim rerun -t rerun` command implies that the HJSON files in the current
    # working directory (or a directory structure msim is aware of) are modified.
    # The `msum.py` approach of creating `temp_dir_path = os.path.join(self.prj_icdir, 'dv', 'sim_ctrl', 'ts', 'temp')`
    # and then running `msim` from a script suggests that `msim` is run from `dv/sim_ctrl/ts` or similar.

    # Command construction
    # The target group is 'rerun' as per msum.py logic
    command_parts = [
        'msim', 'rerun', '-t', 'rerun',
        rebuild_opt, wave_opt, coverage_opt, run_time_opt, dir_opt,
        elab_opt_str, vlogan_opt_str, run_opt_str
    ]
    command = ' '.join(filter(None, command_parts)) # Filter out empty strings

    # The working directory for msim is important.
    # msum.py generates a script, which might cd or assume a CWD.
    # Let's assume CWD should be where `all.hjson` (the master one) is, or where `msim` expects to find it.
    # This is typically PRJ_ICDIR/dv/sim_ctrl/ts
    msim_cwd = os.path.join(PRJ_ICDIR, 'dv', 'sim_ctrl', 'ts')
    if not os.path.isdir(msim_cwd):
        return False, f"MSIM working directory does not exist: {msim_cwd}"

    print(f"Executing command: {command} in CWD: {msim_cwd}")

    try:
        # Using shell=True can be a security risk if command parts are from untrusted input.
        # Here, options are somewhat controlled. For production, sanitize inputs.
        # It's often needed for complex commands or if msim is an alias/script.
        process = subprocess.Popen(command, shell=True, cwd=msim_cwd, 
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate() # Wait for completion

        if process.returncode == 0:
            print(f"MSIM Rerun STDOUT:\n{stdout}")
            return True, "MSIM rerun command executed successfully."
        else:
            print(f"MSIM Rerun STDERR:\n{stderr}")
            print(f"MSIM Rerun STDOUT:\n{stdout}")
            return False, f"MSIM rerun command failed with exit code {process.returncode}. Check server console for msim logs."
    except Exception as e:
        return False, f"Failed to execute msim command: {str(e)}"
    finally:
        # Clean up the temporary HJSON directory
        if os.path.exists(temp_rerun_dir):
            print(f"Cleaning up temporary directory: {temp_rerun_dir}")
            # shutil.rmtree(temp_rerun_dir) # Enable this after thorough testing

# --- Flask Routes ---
@app.route('/')
def index():
    # Serve the main HTML file
    # Assumes interactive_live_report.html is in the same directory as this script
    # or in a 'templates' subdirectory. For simplicity, let's assume same directory.
    return send_from_directory('.', 'interactive_live_report.html')

@app.route('/rerun', methods=['POST'])
def handle_rerun():
    if not PRJ_ICDIR:
         return jsonify({'status': 'error', 'message': 'PRJ_ICDIR environment variable not set on server.'}), 500
        
    data = request.json
    print("Received data for rerun:", data) # Log received data

    selected_cases = data.get('selectedCases', [])
    if not selected_cases:
        return jsonify({'status': 'error', 'message': 'No test cases selected for rerun.'}), 400

    # Create a unique temporary directory for this rerun's HJSON files
    # This should be inside PRJ_ICDIR/dv/sim_ctrl/ts/temp as per msum.py
    # to ensure msim can find the modified files correctly.
    base_temp_dir = os.path.join(PRJ_ICDIR, 'dv', 'sim_ctrl', 'ts', 'temp_reruns')
    if not os.path.exists(base_temp_dir):
        os.makedirs(base_temp_dir, exist_ok=True)
    
    # Create a unique subdirectory for this specific rerun job
    # For simplicity, using a fixed name for now, but a timestamp or UUID would be better for concurrent use.
    # This needs to be unique if multiple users or reruns can happen.
    # For now, let's use a simple fixed name, assuming single-user sequential operation.
    # This is a simplification.
    current_rerun_temp_hjson_dir = os.path.join(base_temp_dir, "current_rerun_hjsons")
    if os.path.exists(current_rerun_temp_hjson_dir):
        shutil.rmtree(current_rerun_temp_hjson_dir) # Clean up from previous if it exists
    os.makedirs(current_rerun_temp_hjson_dir, exist_ok=True)


    # Execute msim rerun (this is a blocking call for now)
    # For long-running tasks, consider background workers (Celery, RQ) or async Flask.
    # For now, a simple threaded execution might be okay for a single user.
    success, message = execute_msim_rerun(data, current_rerun_temp_hjson_dir)

    if success:
        return jsonify({'status': 'success', 'message': message})
    else:
        return jsonify({'status': 'error', 'message': message}), 500

if __name__ == '__main__':
    if not PRJ_ICDIR:
        print("CRITICAL ERROR: PRJ_ICDIR is not set. The server cannot function correctly.")
        print("Please set the PRJ_ICDIR environment variable before running this server.")
    else:
        print(f"PRJ_ICDIR is set to: {PRJ_ICDIR}")
        print(f"HTML report expected at: interactive_live_report.html (served from script directory)")
        print(f"Rerun temporary HJSONs will be in: {os.path.join(PRJ_ICDIR, 'dv', 'sim_ctrl', 'ts', 'temp_reruns', 'current_rerun_hjsons')}")
        print(f"MSIM commands will be attempted from CWD: {os.path.join(PRJ_ICDIR, 'dv', 'sim_ctrl', 'ts')}")
    
    # Make sure the report output directory exists
    if not os.path.exists(REPORT_OUTPUT_DIRECTORY):
        os.makedirs(REPORT_OUTPUT_DIRECTORY, exist_ok=True)
    print(f"General report output (if any from msim) might go to: {REPORT_OUTPUT_DIRECTORY} or as per -dir option")

    app.run(host='localhost', port=5000, debug=True)
