# AXRM-017 gate accounting, shared by the evidence driver and its controls.
# Callers supply admission checks, evidence paths and the external gate command.
stage() {
    local name=$1 rc
    shift
    wait_window
    identity || { echo "A17_IDENTITY_FAIL before $name"; exit 96; }
    local log="$EV/${A17_EVIDENCE_PREFIX:-AXRM-017-fix2}-$mode$round-$name.txt"
    if [ -e "$log" ]; then echo "A17_REFUSING_OVERWRITE $log"; exit 90; fi
    (
        printf 'COMMAND:'; printf ' %q' "$@"
        printf '\nDATE: %s\nHEAD: %s\nSTATUS:\n%s\n' "$(date -u -Is)" "$(git rev-parse HEAD)" "$(git status --porcelain)"
        "$@"; rc=$?
        printf 'STAGE=%s EXIT=%s END=%s\n' "$name" "$rc" "$(date -u -Is)"
        exit "$rc"
    ) >"$log" 2>&1
    rc=$?
    echo "$rc" >"${log%.txt}.exit"
    echo "A17_STAGE mode=$mode$round name=$name exit=$rc at=$(date -u -Is)"
    test "$rc" = 0 || failed=$((failed + 1))
    return "$rc"
}

run_mutations() {
        ports || exit 1
        for variant in ${A17_VARIANTS:?}; do
            rc=0
            stage "$variant" "$G" "${CI[@]}" uv run python "${A17_MUTANT_DRIVER:-$EV/axrm017-fix2-mutant.py}" "$variant" || rc=$?
            test "$rc" = 1 || { echo "A17_MUTANT_UNEXPECTED_EXIT $variant $rc"; [ "$rc" != 0 ] || failed=$((failed + 1)); continue; }
            failed=$((failed - 1))
        done
}
