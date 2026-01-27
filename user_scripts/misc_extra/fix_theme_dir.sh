#!/usr/bin/env bash
# ==============================================================================
# Script Name: theme-activator.sh
# Description: Manages 'dark'/'light' wallpaper directory states.
#              - Resolves conflicts by moving 'light' to parent.
#              - Renames candidate to 'active'.
# Environment: Arch Linux / Hyprland (UWSM)
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. Strict Mode & Configuration
# ------------------------------------------------------------------------------
set -euo pipefail
IFS=$'\n\t'

# Absolute Paths (Robustness)
readonly BASE_DIR="${HOME}/Pictures/wallpapers"
readonly PARENT_DIR="${HOME}/Pictures"

# ANSI Colors (Visual Feedback)
readonly C_GREEN=$'\033[0;32m'
readonly C_BLUE=$'\033[0;34m'
readonly C_YELLOW=$'\033[0;33m'
readonly C_RED=$'\033[0;31m'
readonly C_RESET=$'\033[0m'

# ------------------------------------------------------------------------------
# 2. Helper Functions
# ------------------------------------------------------------------------------
log_info()  { printf "%s[INFO]%s  %s\n" "${C_BLUE}" "${C_RESET}" "$1"; }
log_ok()    { printf "%s[OK]%s    %s\n" "${C_GREEN}" "${C_RESET}" "$1"; }
log_warn()  { printf "%s[WARN]%s  %s\n" "${C_YELLOW}" "${C_RESET}" "$1"; }
log_err()   { printf "%s[ERR]%s   %s\n" "${C_RED}" "${C_RESET}" "$1" >&2; exit 1; }

# ------------------------------------------------------------------------------
# 3. Main Logic
# ------------------------------------------------------------------------------
main() {
    # 3a. Privilege & Sanity Check
    if [[ "${EUID}" -eq 0 ]]; then
        log_err "Do not run as root. Run as user to manage ${HOME}."
    fi

    if [[ ! -d "${BASE_DIR}" ]]; then
        log_err "Base directory not found: ${BASE_DIR}"
    fi

    # Define targets using absolute paths
    local dir_dark="${BASE_DIR}/dark"
    local dir_light="${BASE_DIR}/light"
    local dir_active="${BASE_DIR}/active"

    # --------------------------------------------------------------------------
    # Phase 1: Conflict Resolution
    # Rule: If BOTH exist, move 'light' to parent ($HOME/Pictures/)
    # --------------------------------------------------------------------------
    if [[ -d "${dir_dark}" && -d "${dir_light}" ]]; then
        log_info "Conflict detected: Both 'dark' and 'light' exist."

        # Safety Check: Prevent overwriting if ~/Pictures/light already exists
        if [[ -d "${PARENT_DIR}/light" ]]; then
            log_err "Cannot move 'light' to parent: '${PARENT_DIR}/light' already exists."
        fi

        mv "${dir_light}" "${PARENT_DIR}/"
        log_ok "Conflict resolved: Moved 'light' to ${PARENT_DIR}/."
    fi

    # --------------------------------------------------------------------------
    # Phase 2: Activation
    # Rule: Rename 'dark' OR 'light' to 'active'
    # --------------------------------------------------------------------------
    
    # Check 1: Is 'active' already occupied?
    if [[ -d "${dir_active}" ]]; then
        # If 'active' exists AND we have 'dark' or 'light' waiting, we have an ambiguous state.
        if [[ -d "${dir_dark}" || -d "${dir_light}" ]]; then
            log_warn "Ambiguous state: 'active' directory exists, but new candidates are also present."
            log_warn "Please manually verify which theme should be active in ${BASE_DIR}."
            exit 0
        else
            log_ok "Directory 'active' is already set. No pending candidates."
            exit 0
        fi
    fi

    # Check 2: Process candidates
    if [[ -d "${dir_dark}" ]]; then
        mv "${dir_dark}" "${dir_active}"
        log_ok "Renamed 'dark' to 'active'."
    elif [[ -d "${dir_light}" ]]; then
        mv "${dir_light}" "${dir_active}"
        log_ok "Renamed 'light' to 'active'."
    else
        log_warn "No 'dark' or 'light' directories found to process."
    fi
}

# Execute
main "$@"
