import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_DIR = os.path.join(BASE_DIR, "code")

PYTHON_BIN = os.getenv("PIPELINE_PYTHON", sys.executable)
PIPELINE_FAIL_FAST = os.getenv("PIPELINE_FAIL_FAST", "true").lower() == "true"

RUN_COMPILE = os.getenv("PIPELINE_RUN_COMPILE", "true").lower() == "true"
RUN_WIKI_GENERATOR = os.getenv("PIPELINE_RUN_WIKI_GENERATOR", "true").lower() == "true"
RUN_AUTO_LINKER = os.getenv("PIPELINE_RUN_AUTO_LINKER", "true").lower() == "true"
RUN_GHOST_RESOLVER = os.getenv("PIPELINE_RUN_GHOST_RESOLVER", "true").lower() == "true"
RUN_LINTER = os.getenv("PIPELINE_RUN_LINTER", "true").lower() == "true"


def run_step(step_name, script_name, extra_env=None):
    script_path = os.path.join(CODE_DIR, script_name)
    if not os.path.exists(script_path):
        print(f"[SKIP] {step_name}: missing {script_name}", flush=True)
        return False, 0.0

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    print(f"\n=== {step_name} ===", flush=True)
    started = time.time()

    try:
        subprocess.run(
            [PYTHON_BIN, script_path],
            cwd=CODE_DIR,
            env=env,
            check=True,
        )
        elapsed = time.time() - started
        print(f"[OK] {step_name} ({elapsed:.1f}s)", flush=True)
        return True, elapsed
    except subprocess.CalledProcessError as e:
        elapsed = time.time() - started
        print(f"[FAIL] {step_name} exit_code={e.returncode} ({elapsed:.1f}s)", flush=True)
        if PIPELINE_FAIL_FAST:
            raise
        return False, elapsed


def main():
    started = time.time()
    completed = []

    if RUN_COMPILE:
        ok, sec = run_step("Compile raw -> processed", "compile.py")
        completed.append(("compile.py", ok, sec))

    if RUN_WIKI_GENERATOR:
        ok, sec = run_step(
            "Generate wiki concepts/pages",
            "wiki_generator.py",
            extra_env={
                "WIKI_RUN_AUTO_LINKER": "false",
                "WIKI_RUN_GHOST_RESOLVER": "false",
                "WIKI_RUN_KNOWLEDGE_LINTER": "false",
            },
        )
        completed.append(("wiki_generator.py", ok, sec))

    if RUN_AUTO_LINKER:
        ok, sec = run_step("Auto-link wiki", "auto_linker.py")
        completed.append(("auto_linker.py", ok, sec))

    if RUN_GHOST_RESOLVER:
        ok, sec = run_step("Resolve ghost concepts", "resolve_ghost_concepts.py")
        completed.append(("resolve_ghost_concepts.py", ok, sec))

        if RUN_AUTO_LINKER:
            ok, sec = run_step("Auto-link wiki (post-ghost)", "auto_linker.py")
            completed.append(("auto_linker.py (post-ghost)", ok, sec))

    if RUN_LINTER:
        ok, sec = run_step("Run knowledge linter", "knowledge_linter.py")
        completed.append(("knowledge_linter.py", ok, sec))

    total = time.time() - started
    print("\n=== PIPELINE SUMMARY ===", flush=True)
    for name, ok, sec in completed:
        status = "OK" if ok else "FAIL"
        print(f"- {status}: {name} ({sec:.1f}s)", flush=True)
    print(f"Total: {total:.1f}s", flush=True)


if __name__ == "__main__":
    main()
