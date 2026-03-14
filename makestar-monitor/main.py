import subprocess
import sys
import time
import os
import signal
from datetime import datetime

DEADLINE = datetime(2026, 3, 16, 0, 0, 0)
CHECK_INTERVAL = 30

STREAMLIT_PORT = 8501


def is_past_deadline():
    return datetime.now() >= DEADLINE


def start_monitor():
    proc = subprocess.Popen(
        [sys.executable, "monitor.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    print(f"[main] monitor.py started (PID {proc.pid})")
    return proc


def start_dashboard():
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "app.py",
            "--server.port", str(STREAMLIT_PORT),
            "--server.headless", "true",
            "--server.enableCORS", "false",
            "--server.enableXsrfProtection", "false",
        ],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    print(f"[main] app.py (Streamlit) started (PID {proc.pid})")
    return proc


def terminate_process(proc, name):
    if proc and proc.poll() is None:
        print(f"[main] Stopping {name} (PID {proc.pid})...")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"[main] {name} stopped.")


def main():
    if is_past_deadline():
        print("[main] Current time is past the deadline (2026-03-16 00:00). Exiting.")
        sys.exit(0)

    print(f"[main] Starting Makestar Monitor. Deadline: {DEADLINE}")
    print(f"[main] Dashboard will be available at http://localhost:{STREAMLIT_PORT}")

    monitor_proc = start_monitor()
    dashboard_proc = start_dashboard()

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

            if is_past_deadline():
                print("[main] Deadline reached. Shutting down all processes.")
                terminate_process(monitor_proc, "monitor.py")
                terminate_process(dashboard_proc, "app.py (Streamlit)")
                print("[main] All processes stopped. System halted.")
                break

            if monitor_proc.poll() is not None:
                print("[main] monitor.py exited unexpectedly. Restarting...")
                monitor_proc = start_monitor()

            if dashboard_proc.poll() is not None:
                print("[main] app.py exited unexpectedly. Restarting...")
                dashboard_proc = start_dashboard()

    except KeyboardInterrupt:
        print("\n[main] KeyboardInterrupt received. Shutting down...")
        terminate_process(monitor_proc, "monitor.py")
        terminate_process(dashboard_proc, "app.py (Streamlit)")
        print("[main] Done.")


if __name__ == "__main__":
    main()
