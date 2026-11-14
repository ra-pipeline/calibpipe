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

# Installed launchers may wrap Python, so keep their interpreter selection intact.
if [ -z "${CALIBPIPE_PYTHON:-}" ] && command -v calibpipe >/dev/null 2>&1; then
    __calibpipe_out=$(calibpipe env "$@")
    __calibpipe_status=$?
    if [ "$__calibpipe_status" -eq 0 ]; then
        eval "$__calibpipe_out"
    fi
    if [ "$__calibpipe_status" -ne 0 ]; then
        unset __calibpipe_sourced __calibpipe_out __calibpipe_status
        return 1 2>/dev/null || exit 1
    fi
    unset __calibpipe_sourced __calibpipe_out __calibpipe_status
    return 0 2>/dev/null || exit 0
fi
__calibpipe_python="${CALIBPIPE_PYTHON:-}"
__calibpipe_script_dir=$(cd "$(dirname "${BASH_SOURCE:-$0}")" >/dev/null 2>&1 && pwd)
__calibpipe_root=$(cd "$__calibpipe_script_dir/.." >/dev/null 2>&1 && pwd)
__calibpipe_src="$__calibpipe_root/src"
if [ -z "$__calibpipe_python" ]; then
    if [ -x "$__calibpipe_root/.venv/bin/python" ]; then
        __calibpipe_python="$__calibpipe_root/.venv/bin/python"
    else
        __calibpipe_python=python3
    fi
fi
if [ -d "$__calibpipe_src/calibpipe" ]; then
    __calibpipe_out=$(PYTHONPATH="$__calibpipe_src${PYTHONPATH:+:$PYTHONPATH}" "$__calibpipe_python" -m calibpipe.cli env "$@")
else
    __calibpipe_out=$("$__calibpipe_python" -m calibpipe.cli env "$@")
fi

__calibpipe_status=$?

if [ "$__calibpipe_status" -ne 0 ]; then
    unset __calibpipe_sourced __calibpipe_out __calibpipe_status __calibpipe_python __calibpipe_script_dir __calibpipe_root __calibpipe_src
    return 1 2>/dev/null || exit 1
fi

eval "$__calibpipe_out"
unset __calibpipe_sourced __calibpipe_out __calibpipe_status __calibpipe_python __calibpipe_script_dir __calibpipe_root __calibpipe_src
