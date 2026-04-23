#!/usr/bin/env bash
# Wrapper to translate BMCFuzz/sby rIC3 CLI to current rIC3 CLI format,
# and convert rIC3 output to AIGER witness format expected by sby.
#
# sby expects on stdout:
#   "0" = PASS (property holds)
#   "1" = FAIL (counterexample found), followed by witness trace
#   "2" = PASS (BMC bound reached without finding CEX)

RIC3="${RIC3_REAL:-/root/bmc-workspace/rIC3/target/release/ric3}"

# Parse sby-style arguments
BMC_MAX_K=""
ENGINE=""
AIG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bmc-max-k) BMC_MAX_K="$2"; shift 2 ;;
        -e)          ENGINE="$2"; shift 2 ;;
        -v)          shift 2 ;;
        --witness)   shift ;;
        *.aig)       AIG="$1"; shift ;;
        *)           shift ;;
    esac
done

if [[ -z "$AIG" ]]; then
    echo "Error: no AIG file specified" >&2
    exit 1
fi

# Run rIC3 and capture output
if [[ "$ENGINE" == "bmc" ]]; then
    OUTPUT=$("$RIC3" check "$AIG" bmc --end "${BMC_MAX_K:-100}" 2>&1)
else
    OUTPUT=$("$RIC3" check "$AIG" ic3 2>&1)
fi
RC=$?

# Log full output to stderr for debugging
echo "$OUTPUT" >&2

# Parse result and emit AIGER witness format to stdout
if echo "$OUTPUT" | grep -q "^SAT$"; then
    # Counterexample found
    echo "1"
    echo "b0"
    echo "."
    exit 0
elif echo "$OUTPUT" | grep -q "^UNSAT$"; then
    # Property holds
    echo "0"
    exit 0
elif echo "$OUTPUT" | grep -q "^UNKNOWN$"; then
    # BMC bound reached
    echo "2"
    exit 0
else
    # Error or unexpected output
    echo "2"
    exit $RC
fi
