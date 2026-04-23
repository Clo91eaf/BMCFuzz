"""Direct BMC: synthesize AIG once, run rIC3 per property.

Bypasses sby entirely. Steps:
  1. Yosys: RTL → multi-property AIG (one bad output per cover point)
  2. rIC3: check each property with --prop N
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────
RIC3 = os.environ.get("RIC3_PATH", "/root/bmc-workspace/rIC3/target/release/ric3")
WORK = Path("/root/bmc-workspace/BMCFuzz/formal_run")
RTL_DIR = WORK / "rtl"
MODEL_DIR = WORK / "direct_model"
AIG_FILE = MODEL_DIR / "design.aig"
MAX_DEPTH = 89
NUM_POINTS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
PARALLEL = int(sys.argv[2]) if len(sys.argv) > 2 else 4
TIMEOUT_PER_PROP = 300  # seconds


def synth_aig():
    """Synthesize multi-property AIG from RTL (once)."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # Collect RTL files
    rtl_files = sorted(RTL_DIR.glob("*.sv")) + sorted(RTL_DIR.glob("*.v"))
    if not rtl_files:
        print("ERROR: no RTL files found in", RTL_DIR)
        sys.exit(1)

    read_cmds = "\n".join(f"read -formal {f.name}" for f in rtl_files)

    # Yosys script: keep ALL cover points as bad outputs (multi-property AIG)
    # The cover statements are already converted to assert by chformal -assert2assume
    # in BMCFuzz's RTL preparation. We just keep them all as asserts.
    ys_script = f"""\
# Read RTL
{read_cmds}

# Elaborate
prep -top FormalTop

# Keep all cov_count_* as asserts (each becomes a bad output in AIG)
# Convert non-cover asserts to assumes so they don't interfere
chformal -assert2assume c:cov_count_* %n
chformal -remove -assume c:cov_count_*

# Standard formal prep
hierarchy -smtcheck
scc -select; simplemap; select -clear
memory_nordff
async2sync
chformal -assume -early
opt_clean
formalff -setundef -clk2ff -ff2anyinit -hierarchy
chformal -live -fair -cover -remove
opt_clean
setundef -undriven -anyseq
opt -full
flatten
techmap
opt -fast
memory_map -formal
formalff -clk2ff -ff2anyinit
simplemap
dffunmap
abc -g AND -fast
opt_clean
stat
write_aiger -I -B -zinit -no-startoffset {AIG_FILE}
"""

    ys_path = MODEL_DIR / "synth.ys"
    ys_path.write_text(ys_script)

    print(f"Synthesizing multi-property AIG from {len(rtl_files)} RTL files...")
    t0 = time.time()
    r = subprocess.run(
        ["yosys", "-ql", str(MODEL_DIR / "synth.log"), str(ys_path)],
        cwd=str(RTL_DIR),
    )
    elapsed = time.time() - t0

    if r.returncode != 0:
        print(f"ERROR: Yosys synthesis failed (rc={r.returncode})")
        print(f"Check {MODEL_DIR / 'synth.log'}")
        sys.exit(1)

    print(f"AIG synthesized in {elapsed:.1f}s → {AIG_FILE}")

    # Count properties in AIG
    with open(AIG_FILE, "r") as f:
        header = f.readline().strip().split()
    # AIGER header: aig M I L O B C J F (B = bad outputs = properties)
    if len(header) >= 6:
        n_bad = int(header[5])  # B field
        print(f"AIG has {n_bad} bad outputs (properties)")
        return n_bad
    return 0


def run_bmc_single(prop_id: int) -> dict:
    """Run rIC3 BMC on a single property."""
    t0 = time.time()
    try:
        r = subprocess.run(
            [RIC3, "check", str(AIG_FILE), "bmc",
             "--prop", str(prop_id), "--end", str(MAX_DEPTH)],
            capture_output=True, text=True, timeout=TIMEOUT_PER_PROP,
        )
        elapsed = time.time() - t0
        stdout = r.stdout + r.stderr
        if "SAT" in stdout and "UNSAT" not in stdout:
            status = "sat"
        elif "UNSAT" in stdout:
            status = "unsat"
        else:
            status = "unknown"
        return {"prop": prop_id, "status": status, "wall_s": elapsed}
    except subprocess.TimeoutExpired:
        return {"prop": prop_id, "status": "timeout", "wall_s": TIMEOUT_PER_PROP}
    except Exception as e:
        return {"prop": prop_id, "status": "error", "wall_s": time.time() - t0, "error": str(e)}


def main():
    # Step 1: Synthesize AIG (once)
    if AIG_FILE.exists():
        print(f"AIG already exists: {AIG_FILE}, skipping synthesis")
        with open(AIG_FILE, "r") as f:
            header = f.readline().strip().split()
        n_props = int(header[5]) if len(header) >= 6 else 0
    else:
        n_props = synth_aig()

    if n_props == 0:
        print("ERROR: no properties found in AIG")
        sys.exit(1)

    n = min(NUM_POINTS, n_props)
    print(f"\nRunning BMC on {n}/{n_props} properties, {PARALLEL} parallel workers")

    # Step 2: Run BMC per property
    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(run_bmc_single, i): i for i in range(n)}
        done = 0
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            done += 1
            tag = "SAT" if r["status"] == "sat" else r["status"].upper()
            print(f"  [{done}/{n}] prop {r['prop']:>5d}: {tag} ({r['wall_s']:.1f}s)")

    total_time = time.time() - t0
    results.sort(key=lambda x: x["prop"])

    # Summary
    sat_count = sum(1 for r in results if r["status"] == "sat")
    unsat_count = sum(1 for r in results if r["status"] == "unsat")
    timeout_count = sum(1 for r in results if r["status"] == "timeout")
    unknown_count = sum(1 for r in results if r["status"] == "unknown")

    print(f"\n=== Results ===")
    print(f"Total: {n} properties, {total_time:.1f}s")
    print(f"  SAT (covered):   {sat_count}")
    print(f"  UNSAT:           {unsat_count}")
    print(f"  Timeout:         {timeout_count}")
    print(f"  Unknown:         {unknown_count}")

    # Save
    out = Path("/root/bmc-workspace/output")
    out.mkdir(exist_ok=True)
    outfile = out / "bmc_direct_baseline.json"
    with open(outfile, "w") as f:
        json.dump({
            "num_props": n,
            "sat": sat_count,
            "unsat": unsat_count,
            "timeout": timeout_count,
            "total_time_s": total_time,
            "synth_aig": str(AIG_FILE),
            "results": results,
        }, f, indent=2)
    print(f"Saved to {outfile}")


if __name__ == "__main__":
    main()
