"""
sandbox.py — Polyglot Execution Engine.

Supports Python, C++, and Java.
Each language variant is compiled ONCE (where applicable), then stress-tested
N times to surface heisenbugs, race conditions, and non-determinism.

Language detection:
  - C++  : file contains '#include' or 'int main()'
  - Java : file contains 'public class' or 'public static void main'
  - Python: everything else

C++  requirement: `g++`   must be on PATH  (g++ -std=c++17 compile + binary run)
Java requirement: `javac` + `java` must be on PATH  (javac compile + java run)
"""

import subprocess
import sys
import tempfile
import os
import json
import signal
import re


# ── Python runner suffix ────────────────────────────────────────────────────
# Appended to every Python variant so main() is called in a controlled way.
# Handles both synchronous and async main() functions transparently:
#   - If main() returns a coroutine (i.e. it was declared `async def main()`),
#     we run it with asyncio.run() instead of calling it directly.
#   - This prevents "coroutine was never awaited" crashes when the Manager
#     assigns an asyncio-based approach to the Python generator.
_PYTHON_RUNNER_SUFFIX = """
if __name__ == "__main__":
    import multiprocessing as _mp
    import json as _json
    import inspect as _inspect
    _mp.freeze_support()
    if _inspect.iscoroutinefunction(main):
        import asyncio as _asyncio
        _result = _asyncio.run(main())
    else:
        _result = main()
    print(_json.dumps(_result, sort_keys=True))
"""

_PYTHON = sys.executable

# ── Local build root ─────────────────────────────────────────────────────────
# C++ and Java binaries/class-files are built here instead of AppData\Local\Temp.
# Windows Defender and AppLocker are far more lenient with executables that
# appear inside a developer's project workspace than with ones that pop up
# inside the system temp folder.
#
# Layout:  <project root>/_sandbox_builds/<lang>_<unique_id>/
#
# The directory is deleted after every stress_test() call.
_LOCAL_BUILD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sandbox_builds")


import contextlib
import shutil
import uuid

@contextlib.contextmanager
def _local_build_dir(lang: str):
    """
    Context manager that creates   _sandbox_builds/<lang>_<uuid>/
    next to sandbox.py, yields the path, then deletes it on exit.

    Using a local directory prevents Windows Defender / AppLocker from
    blocking newly compiled C++ (.exe) or Java (.class) files that appear
    in the system temp folder (C:\\Users\\...\\AppData\\Local\\Temp).
    """
    os.makedirs(_LOCAL_BUILD_ROOT, exist_ok=True)
    build_dir = os.path.join(_LOCAL_BUILD_ROOT, f"{lang}_{uuid.uuid4().hex}")
    os.makedirs(build_dir)
    try:
        yield build_dir
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _kill_proc_tree(pid: int) -> None:
    """
    Kill a process and all its children.
    On Windows, subprocess spawns worker processes that must be killed explicitly;
    otherwise they become zombies after a timeout.
    """
    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except Exception:
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def _detect_language(code: str) -> str:
    """Infer language from code structure. Returns 'python', 'c++', or 'java'."""
    if re.search(r'public\s+class\s+\w+', code) or "public static void main" in code:
        return "java"
    if "#include" in code or re.search(r'\bint\s+main\s*\(', code):
        return "c++"
    return "python"


def _get_java_class_name(code: str) -> str:
    """Extract the public class name so the .java filename matches."""
    match = re.search(r'public\s+(?:final\s+)?class\s+(\w+)', code)
    return match.group(1) if match else "Main"


def _check_tool(cmd: str, flag: str) -> bool:
    """Return True if `cmd flag` exits without FileNotFoundError."""
    try:
        subprocess.run([cmd, flag], capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _parse_last_json_line(stdout: str) -> str | None:
    """
    Return the last non-empty line of stdout if it is valid JSON, else None.
    Stray print() calls before the final JSON are silently ignored.
    """
    lines = [l for l in stdout.splitlines() if l.strip()]
    if not lines:
        return None
    last = lines[-1]
    try:
        json.loads(last)
        return last
    except json.JSONDecodeError:
        return None


# ── Per-language stress-testers ──────────────────────────────────────────────

def _stress_test_python(code: str, runs: int, timeout: int) -> dict:
    """
    Write Python code + runner suffix to a temp file once.
    Execute it `runs` times without re-writing the file.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        f.write(_PYTHON_RUNNER_SUFFIX)
        tmp_path = f.name

    successes = 0
    errors: list[str] = []
    all_outputs: list[str] = []
    stderr_warnings: list[str] = []

    try:
        for i in range(runs):
            try:
                proc = subprocess.Popen(
                    [_PYTHON, tmp_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_proc_tree(proc.pid)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    errors.append(f"Run {i}: TIMEOUT — possible deadlock")
                    continue

                if proc.returncode == 0:
                    line = _parse_last_json_line(stdout)
                    if line:
                        successes += 1
                        all_outputs.append(line)
                        if stderr and stderr.strip():
                            stderr_warnings.append(f"Run {i}: {stderr.strip()[:200]}")
                    else:
                        last = stdout.strip().splitlines()[-1][:120] if stdout.strip() else "(empty)"
                        errors.append(f"Run {i}: output not JSON — got: {last}")
                else:
                    errs = (stderr or "").strip().splitlines()
                    errors.append(f"Run {i}: {errs[-1][:150] if errs else 'empty output'}")
            except Exception as e:
                errors.append(f"Run {i}: sandbox error — {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    unique = list(dict.fromkeys(all_outputs))
    return {
        "language": "python",
        "successes": successes,
        "total_runs": runs,
        "sample_outputs": [json.loads(o) for o in unique[:5]],
        "all_raw_outputs": all_outputs,
        "unique_output_count": len(unique),
        "errors": errors,
        "stderr_warnings": stderr_warnings,
        "is_stable": successes == runs and len(unique) <= 1,
    }


def _stress_test_cpp(code: str, runs: int, timeout: int) -> dict:
    """
    Compile the C++ source once with `g++ -std=c++17`, then run the binary N times.
    Unused #includes are warnings (not errors), so the LLM has much more headroom
    than it did with Go's strict unused-import rule.
    """
    with _local_build_dir("cpp") as tmpdir:
        cpp_file = os.path.join(tmpdir, "main.cpp")
        binary   = os.path.join(tmpdir, "solution" + (".exe" if sys.platform == "win32" else ""))

        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write(code)

        # ── Compile once ──────────────────────────────────────────────────
        try:
            build = subprocess.run(
                ["g++", "-std=c++17", "-O2", "-o", binary, cpp_file],
                capture_output=True, text=True, encoding="utf-8",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "language": "c++",
                "successes": 0,
                "total_runs": runs,
                "sample_outputs": [],
                "all_raw_outputs": [],
                "unique_output_count": 0,
                "errors": ["BUILD TIMEOUT: g++ exceeded 60 s"],
                "is_stable": False,
            }
        if build.returncode != 0:
            # Include both stdout and stderr — g++ puts errors on stderr,
            # but some warnings/notes land on stdout.
            err = (build.stderr or build.stdout or "").strip()[:300]
            return {
                "language": "c++",
                "successes": 0,
                "total_runs": runs,
                "sample_outputs": [],
                "all_raw_outputs": [],
                "unique_output_count": 0,
                "errors": [f"BUILD ERROR: {err}"],
                "is_stable": False,
            }

        # ── Run N times ───────────────────────────────────────────────────
        successes = 0
        errors: list[str] = []
        all_outputs: list[str] = []
        stderr_warnings: list[str] = []

        for i in range(runs):
            try:
                proc = subprocess.Popen(
                    [binary],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_proc_tree(proc.pid)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    errors.append(f"Run {i}: TIMEOUT — possible infinite loop")
                    continue

                if proc.returncode == 0:
                    line = _parse_last_json_line(stdout)
                    if line:
                        successes += 1
                        all_outputs.append(line)
                        if stderr and stderr.strip():
                            stderr_warnings.append(f"Run {i}: {stderr.strip()[:200]}")
                    else:
                        last = stdout.strip().splitlines()[-1][:120] if stdout.strip() else "(empty)"
                        errors.append(f"Run {i}: output not JSON — got: {last}")
                else:
                    errs = (stderr or "").strip().splitlines()
                    errors.append(f"Run {i}: {errs[-1][:150] if errs else 'non-zero exit'}")
            except Exception as e:
                errors.append(f"Run {i}: sandbox error — {e}")

        unique = list(dict.fromkeys(all_outputs))
        return {
            "language": "c++",
            "successes": successes,
            "total_runs": runs,
            "sample_outputs": [json.loads(o) for o in unique[:5]],
            "all_raw_outputs": all_outputs,
            "unique_output_count": len(unique),
            "errors": errors,
            "stderr_warnings": stderr_warnings,
            "is_stable": successes == runs and len(unique) <= 1,
        }


def _stress_test_java(code: str, runs: int, timeout: int) -> dict:
    """
    Compile the Java source once with `javac`, then run the class N times with `java`.
    The TemporaryDirectory stays alive across all runs so class files remain accessible.
    """
    class_name = _get_java_class_name(code)

    with _local_build_dir("java") as tmpdir:
        java_file = os.path.join(tmpdir, f"{class_name}.java")

        with open(java_file, "w", encoding="utf-8") as f:
            f.write(code)

        # ── Compile once ──────────────────────────────────────────────────
        try:
            compile_proc = subprocess.run(
                ["javac", java_file],
                capture_output=True, text=True, encoding="utf-8",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "language": "java",
                "successes": 0,
                "total_runs": runs,
                "sample_outputs": [],
                "all_raw_outputs": [],
                "unique_output_count": 0,
                "errors": ["COMPILE TIMEOUT: javac exceeded 60 s"],
                "is_stable": False,
            }
        if compile_proc.returncode != 0:
            err = compile_proc.stderr.strip()[:200]
            return {
                "language": "java",
                "successes": 0,
                "total_runs": runs,
                "sample_outputs": [],
                "all_raw_outputs": [],
                "unique_output_count": 0,
                "errors": [f"COMPILE ERROR: {err}"],
                "is_stable": False,
            }

        # ── Run N times ───────────────────────────────────────────────────
        successes = 0
        errors: list[str] = []
        all_outputs: list[str] = []
        stderr_warnings: list[str] = []

        for i in range(runs):
            try:
                proc = subprocess.Popen(
                    ["java", "-cp", tmpdir, class_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_proc_tree(proc.pid)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    errors.append(f"Run {i}: TIMEOUT — thread deadlock or livelock")
                    continue

                if proc.returncode == 0:
                    line = _parse_last_json_line(stdout)
                    if line:
                        successes += 1
                        all_outputs.append(line)
                        if stderr and stderr.strip():
                            stderr_warnings.append(f"Run {i}: {stderr.strip()[:200]}")
                    else:
                        last = stdout.strip().splitlines()[-1][:120] if stdout.strip() else "(empty)"
                        errors.append(f"Run {i}: output not JSON — got: {last}")
                else:
                    errs = (stderr or "").strip().splitlines()
                    errors.append(f"Run {i}: {errs[-1][:150] if errs else 'empty output'}")
            except Exception as e:
                errors.append(f"Run {i}: sandbox error — {e}")

        unique = list(dict.fromkeys(all_outputs))
        return {
            "language": "java",
            "successes": successes,
            "total_runs": runs,
            "sample_outputs": [json.loads(o) for o in unique[:5]],
            "all_raw_outputs": all_outputs,
            "unique_output_count": len(unique),
            "errors": errors,
            "stderr_warnings": stderr_warnings,
            "is_stable": successes == runs and len(unique) <= 1,
        }


# ── Public API ───────────────────────────────────────────────────────────────

def stress_test(code: str, runs: int = 30, timeout: int = 15, language: str = None) -> dict:
    """
    Dispatch to the correct per-language stress tester.

    Args:
        code     : Source code string (Python, C++, or Java).
        runs     : Number of times to execute the code.
        timeout  : Per-run timeout in seconds.
        language : 'python' | 'c++' | 'java'. Auto-detected if None.

    Returns a dict with: language, successes, total_runs, sample_outputs,
    unique_output_count, errors, is_stable.
    """
    if language is None:
        language = _detect_language(code)

    # ── Tool availability checks ──────────────────────────────────────────
    if language == "c++" and not _check_tool("g++", "--version"):
        return {
            "language": "c++",
            "successes": 0,
            "total_runs": runs,
            "sample_outputs": [],
            "all_raw_outputs": [],
            "unique_output_count": 0,
            "errors": ["C++ compiler (g++) not found on PATH — install MinGW-w64 (Windows) or build-essential (Linux)"],
            "is_stable": False,
        }

    if language == "java" and not _check_tool("javac", "-version"):
        return {
            "language": "java",
            "successes": 0,
            "total_runs": runs,
            "sample_outputs": [],
            "all_raw_outputs": [],
            "unique_output_count": 0,
            "errors": ["Java compiler (javac) not found on PATH — install JDK from https://adoptium.net"],
            "is_stable": False,
        }

    if language == "c++":
        return _stress_test_cpp(code, runs, timeout)
    if language == "java":
        return _stress_test_java(code, runs, timeout)
    return _stress_test_python(code, runs, timeout)