#!/bin/sh
# Mediatovideo Converter macOS prerequisite installer and launcher.
#
# This script never decides which Python, Tk or FFmpeg versions are acceptable:
# it asks scripts/check_runtime.py (Python/Tk policy from
# mediatovideo_converter/runtime.py) and "run_app.py --check-video-tools"
# (FFmpeg policy from mediatovideo_converter/converter.py). Its own job is to
# enumerate candidate paths and to install or update the version-qualified
# Homebrew formulas when no candidate passes.

if [ -z "${SCRIPT_DIRECTORY:-}" ]; then
    SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
CHECK_RUNTIME_SCRIPT="$SCRIPT_DIRECTORY/scripts/check_runtime.py"

STEP_NUMBER=0
PYTHON_COMMAND=""
CHECK_PYTHON=""
LAST_PROBE_OUTPUT=""
REQUIREMENTS_TEXT="the required Python and Tk versions"

installer_header() {
    clear
    printf '%s\n' '============================================================'
    printf '%s\n' ' Mediatovideo Converter - macOS startup'
    printf '%s\n' '============================================================'
    printf '%s\n' 'This window checks and installs the required components.'
    printf '%s\n\n' 'It remains open so progress and errors are always visible.'
}

installer_step() {
    STEP_NUMBER=$((STEP_NUMBER + 1))
    printf '[%s] %s\n' "$STEP_NUMBER" "$1"
}

installer_success() {
    printf '    OK: %s\n' "$1"
}

installer_error() {
    stage=$1
    problem=$2
    action=$3
    details=${4:-}
    printf '\n%s\n' '============================================================'
    printf '%s\n' ' STARTUP ERROR'
    printf '%s\n' '============================================================'
    printf 'Stage:   %s\n' "$stage"
    printf 'Problem: %s\n' "$problem"
    if [ -n "$details" ]; then
        printf 'Details: %s\n' "$details"
    fi
    printf 'What to do: %s\n\n' "$action"
    return 1
}

installer_load_homebrew() {
    if command -v brew >/dev/null 2>&1; then
        return 0
    fi
    for brew_path in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -x "$brew_path" ]; then
            eval "$("$brew_path" shellenv)"
            return 0
        fi
    done
    return 1
}

installer_homebrew_prefix() {
    if installer_load_homebrew; then
        brew --prefix
    fi
}

# --- Packaged (self-contained) build preference ---------------------------

installer_packaged_app_path() {
    # A frozen build carries its own Python, Tk and video tools, so it is always
    # preferred over an install that would touch the user's Homebrew setup.
    for candidate in \
        "$SCRIPT_DIRECTORY/dist/Mediatovideo Converter.app/Contents/MacOS/Mediatovideo Converter" \
        "$SCRIPT_DIRECTORY/Mediatovideo Converter.app/Contents/MacOS/Mediatovideo Converter" \
        "$SCRIPT_DIRECTORY/dist/Mediatovideo Converter"
    do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# --- Runtime probing (policy lives in scripts/check_runtime.py) ------------

installer_checker_hosts() {
    # The checker must stay runnable by the old system Python; it only drives
    # subprocess probes of the real candidates.
    printf '%s\n' /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3
    command -v python3 2>/dev/null
    command -v python3.14 2>/dev/null
}

installer_run_checker() {
    # Runs the checker with the first host interpreter that can execute it.
    # Exit 0 = supported, 1 = unsupported, 127 = no usable host interpreter.
    for host in $(installer_checker_hosts); do
        if [ ! -x "$host" ]; then
            continue
        fi
        output=$("$host" "$CHECK_RUNTIME_SCRIPT" "$@" 2>&1)
        status=$?
        if [ "$status" -eq 0 ] || [ "$status" -eq 1 ]; then
            CHECK_PYTHON=$host
            printf '%s\n' "$output"
            return "$status"
        fi
    done
    return 127
}

installer_refresh_requirements_text() {
    text=$(installer_run_checker --print-requirements 2>/dev/null)
    if [ -n "$text" ]; then
        REQUIREMENTS_TEXT=$text
    fi
}

installer_probe_python() {
    candidate=$1
    installer_probe_output=$(installer_run_checker --check-runtime --python "$candidate")
    installer_probe_status=$?
    LAST_PROBE_OUTPUT=$installer_probe_output
    if [ "$installer_probe_status" -eq 0 ]; then
        return 0
    fi
    return 1
}

installer_python_candidates() {
    # Version-qualified Homebrew locations first (both Intel and Apple Silicon
    # prefixes), then the python.org framework installer, then generic names.
    brew_prefix=$(installer_homebrew_prefix)
    for prefix in "$brew_prefix" /opt/homebrew /usr/local; do
        if [ -z "$prefix" ]; then
            continue
        fi
        printf '%s\n' "$prefix/opt/python@3.14/bin/python3.14"
        printf '%s\n' "$prefix/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
        printf '%s\n' "$prefix/bin/python3.14"
    done
    printf '%s\n' '/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14'
    command -v python3.14 2>/dev/null
    command -v python3 2>/dev/null
    printf '%s\n' /usr/bin/python3
}

installer_find_python() {
    PYTHON_COMMAND=""
    for candidate in $(installer_python_candidates); do
        if [ ! -x "$candidate" ]; then
            continue
        fi
        if installer_probe_python "$candidate"; then
            PYTHON_COMMAND=$candidate
            return 0
        fi
    done
    return 1
}

# --- Installation ---------------------------------------------------------

installer_install_homebrew() {
    installer_step 'Homebrew is required for missing components; installing Homebrew.'
    if ! command -v curl >/dev/null 2>&1; then
        installer_error \
            'Installing Homebrew' \
            'The macOS curl download tool is unavailable.' \
            'Install the macOS Command Line Tools, then run run_macos.command again.'
        return 1
    fi
    if ! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
        installer_error \
            'Installing Homebrew' \
            'The official Homebrew installer did not complete.' \
            'Check the internet connection and any password prompt above, then run this launcher again.'
        return 1
    fi
    if ! installer_load_homebrew; then
        installer_error \
            'Verifying Homebrew' \
            'Homebrew finished installing but its brew command could not be found.' \
            'Restart the Mac, then run run_macos.command again.'
        return 1
    fi
    installer_success 'Homebrew is installed and available.'
}

installer_require_homebrew() {
    if installer_load_homebrew; then
        installer_success 'Homebrew is available.'
        return 0
    fi
    installer_install_homebrew
}

installer_python_tk_installed() {
    brew list --versions python-tk@3.14 >/dev/null 2>&1
}

installer_install_python() {
    installer_refresh_requirements_text
    installer_step "Python and Tk were missing or outdated; installing or updating them ($REQUIREMENTS_TEXT)."
    if ! installer_require_homebrew; then
        return 1
    fi

    if ! installer_python_tk_installed; then
        # Fresh install: the version-qualified formula pulls matching Python 3.14.
        if ! brew install python-tk@3.14; then
            installer_error \
                'Installing Python and Tkinter' \
                'Homebrew could not install the python-tk@3.14 formula.' \
                'Check the internet connection, available disk space, and Homebrew error above, then retry.'
            return 1
        fi
    else
        # Already installed but too old or broken: "brew install" would be a
        # no-op, so upgrade the runtime and its Tk binding explicitly.
        brew update >/dev/null 2>&1 || true
        brew upgrade python@3.14 || true
        brew upgrade python-tk@3.14 || true
    fi

    if ! installer_find_python; then
        printf '%s\n' "$LAST_PROBE_OUTPUT"
        installer_error \
            'Verifying Python and Tkinter' \
            'Python and Tk were installed or updated, but the runtime check still fails.' \
            'Run brew update then brew upgrade python@3.14 python-tk@3.14 in Terminal, then run run_macos.command again.'
        return 1
    fi
    installer_success "Python and Tkinter are installed and working at $PYTHON_COMMAND."
}

# --- FFmpeg (version and capability policy lives in converter.py) ---------

installer_find_video_tools() {
    if [ -z "$PYTHON_COMMAND" ]; then
        return 1
    fi
    # Presence is not enough: this verifies the minimum version, encoders and
    # demuxer through the selected interpreter's own converter policy.
    "$PYTHON_COMMAND" "$SCRIPT_DIRECTORY/run_app.py" --check-video-tools >/dev/null 2>&1
}

installer_ffmpeg_installed() {
    brew list --versions ffmpeg >/dev/null 2>&1
}

installer_install_ffmpeg() {
    installer_step 'A compatible FFmpeg/FFprobe pair was not found; installing or updating FFmpeg.'
    if ! installer_require_homebrew; then
        return 1
    fi
    if ! installer_ffmpeg_installed; then
        if ! brew install ffmpeg; then
            installer_error \
                'Installing FFmpeg' \
                'Homebrew could not install FFmpeg.' \
                'Check the internet connection, available disk space, and Homebrew error above, then retry.'
            return 1
        fi
    else
        # An old FFmpeg build would fail the capability check while
        # "brew install" reports success as a no-op, so upgrade explicitly.
        brew update >/dev/null 2>&1 || true
        brew upgrade ffmpeg || true
    fi
    if ! installer_find_video_tools; then
        "$PYTHON_COMMAND" "$SCRIPT_DIRECTORY/run_app.py" --check-video-tools || true
        installer_error \
            'Verifying FFmpeg' \
            'FFmpeg was installed or updated, but it still does not meet the required version and features.' \
            'Run brew update then brew upgrade ffmpeg in Terminal, then run run_macos.command again.'
        return 1
    fi
    installer_success 'FFmpeg and FFprobe are installed and working.'
}

# --- Top level ------------------------------------------------------------

install_macos_prerequisites() {
    installer_refresh_requirements_text

    installer_step "Checking Python and Tkinter ($REQUIREMENTS_TEXT)."
    if installer_find_python; then
        installer_success "Compatible Python and Tkinter found at $PYTHON_COMMAND."
    elif ! installer_install_python; then
        return 1
    fi

    installer_step 'Checking FFmpeg and FFprobe.'
    if installer_find_video_tools; then
        installer_success 'Compatible FFmpeg and FFprobe found.'
    elif ! installer_install_ffmpeg; then
        return 1
    fi
    installer_success 'No additional Python packages are required.'
}

install_macos_and_run() {
    installer_header

    packaged_app=$(installer_packaged_app_path) || packaged_app=""
    if [ -n "$packaged_app" ]; then
        installer_step 'Starting the packaged Mediatovideo Converter build.'
        "$packaged_app"
        exit_code=$?
        if [ "$exit_code" -ne 0 ]; then
            installer_error \
                'Running Mediatovideo Converter' \
                'The packaged application stopped unexpectedly.' \
                'Read the error shown above. Run this launcher again after correcting it.' \
                "Application exit code: $exit_code"
            return 1
        fi
        printf '\n'
        installer_success 'Mediatovideo Converter closed normally.'
        return 0
    fi

    if ! install_macos_prerequisites; then
        return 1
    fi
    installer_step 'Starting Mediatovideo Converter.'
    "$PYTHON_COMMAND" "$SCRIPT_DIRECTORY/run_app.py"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        installer_error \
            'Running Mediatovideo Converter' \
            'The application stopped unexpectedly.' \
            'Read the error shown above. Run this launcher again after correcting it.' \
            "Application exit code: $exit_code"
        return 1
    fi
    printf '\n'
    installer_success 'Mediatovideo Converter closed normally.'
}
