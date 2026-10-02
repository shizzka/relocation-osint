import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

from core import ROOT, now, read_json, write_json


FETCH_STATE = ROOT / "data/out/fetch-state.json"


def run(command, run_id):
    environment = os.environ.copy()
    environment["RELOCATION_RUN_ID"] = run_id
    environment["RELOCATION_PIN_EVIDENCE"] = "1"
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


def current_run_id():
    state = read_json(FETCH_STATE)
    if state.get("run_id") and not state.get("pipeline_complete", False):
        return state["run_id"]
    return uuid.uuid4().hex


def progress_snapshot():
    state = read_json(FETCH_STATE)
    return (
        int(state.get("completed_count", 0)),
        len(read_json(ROOT / "data/out/judged_country_v2.json")),
        len(read_json(ROOT / "data/out/judged_city_v2.json")),
        len(read_json(ROOT / "data/out/master_v2.json")),
    )


def delay_for_rate_limit(state, streak, check_delay, maximum_delay):
    server_delay = state.get("retry_after_seconds")
    if isinstance(server_delay, (int, float)) and server_delay > 0:
        return max(1, int(server_delay)), "по Retry-After/reset от сервера"
    if state:
        delay = min(maximum_delay, check_delay * (2 ** min(max(streak - 1, 0), 10)))
        return max(1, delay), "экспоненциальная проверка без reset-заголовка"
    return max(1, maximum_delay), "нет данных о cooldown; резервный интервал"


def run_resilient(command, run_id):
    maximum_delay = int(os.getenv("RELOCATION_RATE_LIMIT_RETRY_SECONDS", "3600"))
    check_delay = int(os.getenv("RELOCATION_RATE_LIMIT_CHECK_SECONDS", "900"))
    streak = 0
    previous_progress = progress_snapshot()
    while True:
        result = run(command, run_id)
        if result != 75:
            return result
        current_progress = progress_snapshot()
        streak = 1 if current_progress > previous_progress else streak + 1
        previous_progress = current_progress
        state = read_json(ROOT / "data/out/rate-limit.json")
        retry_delay, reason = delay_for_rate_limit(state, streak, check_delay, maximum_delay)
        next_check = datetime.now().astimezone() + timedelta(seconds=retry_delay)
        stage = state.get("stage", "API") if state else "API"
        print(
            f"429 ({stage}); следующая проверка в {next_check:%H:%M:%S} "
            f"через {retry_delay} сек. ({reason}; серия={streak}).",
            flush=True,
        )
        time.sleep(retry_delay)


def main():
    run_id = current_run_id()
    result = run_resilient([sys.executable, "-u", "01_fetch.py"], run_id)
    if result:
        return result
    result = run_resilient([sys.executable, "-u", "02_judge.py"], run_id)
    if result:
        return result
    result = run([sys.executable, "03_excel.py", "--input", str(ROOT / "data/out/master_v2.json"), "--output", str(ROOT / "data/out/Relocation_Master_v2.xlsx")], run_id)
    if result == 0:
        state = read_json(FETCH_STATE)
        state.update({"pipeline_complete": True, "pipeline_completed_at": now(), "updated_at": now()})
        write_json(FETCH_STATE, state)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
