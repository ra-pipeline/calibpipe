#!/bin/sh
# calibpipe_env.sh -- source this to set the ALMA-pipeline environment:
#
#   source scripts/calibpipe_env.sh --env=main
#
# Works from bash or zsh. Must be sourced (`source ...` / `. ...`), not executed.

# Check if script is being sourced
__calibpipe_sourced=0
if [ -n "${BASH_SOURCE:-}" ]; then
    [ "${BASH_SOURCE}" != "${0}" ] && __calibpipe_sourced=1
elif [ -n "${ZSH_VERSION:-}" ]; then
    case "${ZSH_EVAL_CONTEXT:-}" in
        *:file) __calibpipe_sourced=1 ;;
    esac
fi

if [ "$__calibpipe_sourced" -eq 0 ]; then
    echo "calibpipe_env.sh: this script must be sourced, not executed -- run:" >&2
    echo "    source $0 $*" >&2
    exit 1
fi

# Locate Python or calibpipe executable
if command -v calibpipe >/dev/null 2>&1; then
    __calibpipe_cmd="calibpipe env"
else
    # Fallback to python in checkout: locate src directory relative to this script
    __calibpipe_script_dir=$(cd "$(dirname "${BASH_SOURCE:-$0}")" >/dev/null 2>&1 && pwd)
    if [ -d "$__calibpipe_script_dir/src" ]; then
        __calibpipe_src="$__calibpipe_script_dir/src"
    elif [ -d "$__calibpipe_script_dir/../src" ]; then
        __calibpipe_src="$(cd "$__calibpipe_script_dir/.." >/dev/null 2>&1 && pwd)/src"
    else
        __calibpipe_src=""
    fi

    if [ -n "$__calibpipe_src" ]; then
        __calibpipe_cmd="PYTHONPATH=\"$__calibpipe_src:\$PYTHONPATH\" python3 -m calibpipe.cli env"
    else
        __calibpipe_cmd="python3 -m calibpipe.cli env"
    fi
    unset __calibpipe_script_dir __calibpipe_src
fi

__calibpipe_out=$(eval "$__calibpipe_cmd \"\$@\"")
__calibpipe_status=$?

if [ "$__calibpipe_status" -ne 0 ]; then
    unset __calibpipe_sourced __calibpipe_cmd __calibpipe_out __calibpipe_status
    return 1 2>/dev/null || exit 1
fi

eval "$__calibpipe_out"
unset __calibpipe_sourced __calibpipe_cmd __calibpipe_out __calibpipe_status
