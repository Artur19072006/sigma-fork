from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
)

DEFAULT_PROFILE = "school_day"
DEFAULT_TIMEZONE = "Europe/Berlin"
DEFAULT_MAX_ODDSPAPI_REQUESTS_PER_RUN = 4
DEFAULT_MAPPING_PROFILE = MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER
DEFAULT_DOTA2_MAPPING_PROFILE = (
    MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
)
DEFAULT_LOL_MAPPING_PROFILE = MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER
CS2_KEYSET_TAG_IDS = ("100639", "1")
DOTA2_KEYSET_TAG_IDS = ("100639", "1")
LOL_KEYSET_TAG_IDS = ("100639", "1")
DOTA2_MANUAL_PROFILE = "manual_test_dota2"
LOL_MANUAL_PROFILE = "manual_test_lol"
LOL_TOURNAMENT_SCOPE_CHOICES = ("msi", "all")
DEFAULT_LOL_TOURNAMENT_SCOPE = "all"
DEFAULT_DOTA2_POLYMARKET_KEYSET_MAX_PAGES = 12
DEFAULT_DOTA2_POLYMARKET_EMPTY_TARGET_PAGES_STOP = 3
DEFAULT_DOTA2_POLYMARKET_PAGE_PRESEED_URLS = (
    "https://polymarket.com/esports/dota-2",
    "https://polymarket.com/uk/esports/dota-2",
)
DEFAULT_LOL_POLYMARKET_KEYSET_MAX_PAGES = 12
DEFAULT_LOL_POLYMARKET_EMPTY_TARGET_PAGES_STOP = 3


@dataclass(frozen=True)
class ScheduleSlot:
    hour: int
    minute: int
    mode: str
    sports: tuple[str, ...] = ("cs2",)

    @property
    def label(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class DueSlot:
    slot: ScheduleSlot
    scheduled_time: datetime


@dataclass(frozen=True)
class SkippedSlot:
    slot: ScheduleSlot
    scheduled_time: datetime
    reason: str


@dataclass(frozen=True)
class ScanRunResult:
    scheduled_time: datetime
    actual_start_time: datetime
    mode: str
    command: list[str]
    log_file: Path
    returncode: int
    fields: dict[str, str]
    failed: bool
    sport: str = ""
    error: str = ""


def _minute_range(
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
    step_minutes: int,
) -> tuple[tuple[int, int], ...]:
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return tuple(divmod(total, 60) for total in range(start, end, step_minutes))


def _production_schedule(*, include_dota2: bool = False) -> tuple[ScheduleSlot, ...]:
    slots: dict[tuple[int, int], list[str]] = {}

    def add(sport: str, hour: int, minute: int) -> None:
        sports = slots.setdefault((hour, minute), [])
        if sport not in sports:
            sports.append(sport)

    for hour, minute in ((6, 0), (6, 15)):
        add("cs2", hour, minute)
        add("lol", hour, minute)
        if include_dota2:
            add("dota2", hour, minute)

    for hour, minute in _minute_range(8, 0, 20, 0, 30):
        add("cs2", hour, minute)
    for hour, minute in _minute_range(20, 0, 23, 0, 8):
        add("cs2", hour, minute)

    for hour, minute in _minute_range(8, 0, 20, 0, 90):
        add("lol", hour, minute)
    for hour, minute in _minute_range(20, 0, 23, 0, 30):
        add("lol", hour, minute)

    if include_dota2:
        for hour, minute in _minute_range(8, 0, 20, 0, 90):
            add("dota2", hour, minute)
        for hour, minute in _minute_range(20, 0, 23, 0, 30):
            add("dota2", hour, minute)

    sport_order = {"cs2": 0, "lol": 1, "dota2": 2}
    return tuple(
        ScheduleSlot(
            hour,
            minute,
            "normal",
            tuple(sorted(sports, key=lambda sport: sport_order.get(sport, 99))),
        )
        for (hour, minute), sports in sorted(slots.items())
    )


SCHEDULES: dict[str, tuple[ScheduleSlot, ...]] = {
    "school_day": _production_schedule(include_dota2=False),
    "evening_only": (
        ScheduleSlot(21, 0, "normal"),
        ScheduleSlot(21, 30, "cache-only"),
        ScheduleSlot(22, 0, "cache-only"),
        ScheduleSlot(22, 30, "normal"),
        ScheduleSlot(23, 0, "cache-only"),
        ScheduleSlot(23, 30, "normal"),
    ),
    "manual_test": (),
    DOTA2_MANUAL_PROFILE: (),
    LOL_MANUAL_PROFILE: (),
}


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    requested_sport = canonical_sport(args.sport)
    if (
        requested_sport == "lol"
        and args.profile != LOL_MANUAL_PROFILE
        and not profile_schedules_sport(args.profile, "lol")
    ):
        parser.error(
            "LoL scans are only allowed with --profile school_day "
            "or --profile manual_test_lol"
        )
    if (
        requested_sport == "dota2"
        and args.profile != DOTA2_MANUAL_PROFILE
        and not profile_schedules_sport(args.profile, "dota2")
    ):
        parser.error(
            "Dota2 scans are only allowed with --profile manual_test_dota2"
        )
    scheduler_tz = resolve_timezone(args.timezone)

    if args.list_schedule:
        print(format_schedule(args.profile, timezone_name=args.timezone))
        return 0

    if args.dry_run:
        print_dry_run_plan(args)
        return 0

    if args.once or args.profile in {
        "manual_test",
        DOTA2_MANUAL_PROFILE,
        LOL_MANUAL_PROFILE,
    }:
        mode = default_mode(args.profile)
        scheduled_time = datetime.now(scheduler_tz)
        result = run_scan_once(
            args=args,
            scheduled_time=scheduled_time,
            mode=mode,
            now_func=lambda: datetime.now(scheduler_tz),
        )
        print_run_summary(result)
        return result.returncode

    return run_scheduler_loop(args, scheduler_tz=scheduler_tz)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run scheduled esports Stake/OddsPapi scans and Telegram alerts."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(SCHEDULES),
        default=DEFAULT_PROFILE,
    )
    parser.add_argument(
        "--sport",
        choices=[
            "cs2",
            "dota",
            "dota2",
            "dota-2",
            "lol",
            "league_of_legends",
            "league-of-legends",
        ],
        default="cs2",
    )
    parser.add_argument("--bookmaker", choices=["stake"], default="stake")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--list-schedule", action="store_true")
    parser.add_argument(
        "--polymarket-keyset-tag-id",
        action="append",
        help="Polymarket keyset tag id to pass through to scan.py.",
    )
    parser.add_argument(
        "--lol-tournament-scope",
        choices=LOL_TOURNAMENT_SCOPE_CHOICES,
        default=DEFAULT_LOL_TOURNAMENT_SCOPE,
        help="LoL manual profile tournament scope; msi opts back into MSI-only.",
    )
    parser.add_argument("--telegram-alert", action="store_true")
    parser.add_argument("--telegram-dry-run", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--sleep-poll-seconds", type=float, default=30.0)
    parser.add_argument("--run-missed-window-minutes", type=float, default=5.0)
    parser.add_argument("--no-catch-up", action="store_true")
    return parser


def get_schedule(profile: str) -> tuple[ScheduleSlot, ...]:
    try:
        return SCHEDULES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown schedule profile: {profile}") from exc


def profile_schedules_sport(profile: str, sport: str) -> bool:
    return any(sport in slot.sports for slot in get_schedule(profile))


def default_mode(profile: str) -> str:
    schedule = get_schedule(profile)
    if schedule:
        return schedule[0].mode
    return "normal"


def format_schedule(profile: str, *, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    lines = [f"profile={profile}", f"timezone={timezone_name}", "schedule:"]
    schedule = get_schedule(profile)
    if not schedule:
        lines.append("immediate normal")
        return "\n".join(lines)
    for slot in schedule:
        lines.append(f"{slot.label} {slot.mode} sports={','.join(slot.sports)}")
    return "\n".join(lines)


def print_dry_run_plan(args: argparse.Namespace) -> None:
    print(f"dry_run=true")
    print(format_schedule(args.profile, timezone_name=args.timezone))
    slots = get_schedule(args.profile)
    if args.once or args.profile == "manual_test" or not slots:
        mode = default_mode(args.profile)
        print(f"planned_command={format_command(build_scan_command(args, mode=mode))}")
        return
    for slot in slots:
        for sport in slot.sports:
            command = build_scan_command(args, mode=slot.mode, sport=sport)
            print(
                f"planned_run={slot.label} {slot.mode} sport={sport} "
                f"{format_command(command)}"
            )


def build_scan_command(
    args: argparse.Namespace,
    *,
    mode: str,
    sport: str | None = None,
) -> list[str]:
    sport = canonical_sport(sport) if sport is not None else profile_sport(args)
    keyset_tag_ids = profile_keyset_tag_ids(args, sport=sport)
    manual_esports_sport = sport in {"dota2", "lol"}
    keyset_max_pages = "5"
    if sport == "dota2":
        keyset_max_pages = str(DEFAULT_DOTA2_POLYMARKET_KEYSET_MAX_PAGES)
    if sport == "lol":
        keyset_max_pages = str(DEFAULT_LOL_POLYMARKET_KEYSET_MAX_PAGES)
    command = [
        sys.executable,
        "-m",
        "provider_odds.cli.scan",
        "--sport",
        sport,
        "--bookmaker",
        args.bookmaker,
        "--stake-auto-discover-tournaments",
        "--stake-auto-max-tournaments",
        "12" if manual_esports_sport else "30",
        "--stake-request-delay-seconds",
        "5",
        "--stake-rate-limit-retries",
        "2",
        "--polymarket-keyset-window-hours",
        "120",
        "--polymarket-keyset-limit",
        "100",
        "--polymarket-keyset-max-pages",
        keyset_max_pages,
        "--min-tournament-quality",
        "b",
        "--max-selected-tournament-ids-per-sport",
        "5" if manual_esports_sport else "10",
        "--max-oddspapi-requests-per-run",
        str(DEFAULT_MAX_ODDSPAPI_REQUESTS_PER_RUN),
        "--find-arbitrage",
        "--polymarket-fee-rate",
        "0.03",
        "--format",
        "table",
        "--debug",
        "--tournament-resolution-mode",
        mode,
    ]
    if sport == "cs2":
        command.extend(["--stake-mapping-profile", DEFAULT_MAPPING_PROFILE])
    if sport == "dota2":
        command.extend(
            [
                "--stake-mapping-profile",
                DEFAULT_DOTA2_MAPPING_PROFILE,
                "--polymarket-page-preseed-dota2-defaults",
                "--polymarket-keyset-adaptive",
                "--polymarket-keyset-empty-target-pages-stop",
                str(DEFAULT_DOTA2_POLYMARKET_EMPTY_TARGET_PAGES_STOP),
            ]
        )
    if sport == "lol":
        command.extend(
            [
                "--stake-mapping-profile",
                DEFAULT_LOL_MAPPING_PROFILE,
                "--lol-tournament-scope",
                args.lol_tournament_scope,
                "--polymarket-keyset-adaptive",
                "--polymarket-keyset-empty-target-pages-stop",
                str(DEFAULT_LOL_POLYMARKET_EMPTY_TARGET_PAGES_STOP),
            ]
        )
    for tag_id in keyset_tag_ids:
        command.extend(["--polymarket-keyset-tag-id", tag_id])
    if profile_sends_telegram_by_default(args.profile) or args.telegram_alert:
        command.append("--telegram-alert")
    if args.telegram_dry_run or profile_uses_telegram_dry_run_by_default(args.profile):
        command.append("--telegram-dry-run")
    return command


def profile_sport(args: argparse.Namespace) -> str:
    if args.profile == DOTA2_MANUAL_PROFILE:
        return "dota2"
    if args.profile == LOL_MANUAL_PROFILE:
        return "lol"
    return canonical_sport(args.sport)


def canonical_sport(value: str) -> str:
    normalized = re.sub(r"[\s_-]+", " ", str(value).strip().casefold())
    if normalized in {"dota", "dota2", "dota 2"}:
        return "dota2"
    if normalized in {"lol", "league of legends"}:
        return "lol"
    return "cs2"


def profile_keyset_tag_ids(args: argparse.Namespace, *, sport: str) -> tuple[str, ...]:
    if args.polymarket_keyset_tag_id:
        return tuple(_split_csv_values(args.polymarket_keyset_tag_id))
    if sport == "cs2":
        return CS2_KEYSET_TAG_IDS
    if sport == "dota2":
        return DOTA2_KEYSET_TAG_IDS
    if sport == "lol":
        return LOL_KEYSET_TAG_IDS
    return ()


def profile_sends_telegram_by_default(profile: str) -> bool:
    return profile not in {DOTA2_MANUAL_PROFILE, LOL_MANUAL_PROFILE}


def profile_uses_telegram_dry_run_by_default(profile: str) -> bool:
    return profile == DOTA2_MANUAL_PROFILE


def _split_csv_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value).split(","):
            clean = part.strip()
            key = clean.casefold()
            if clean and key not in seen:
                seen.add(key)
                result.append(clean)
    return result


def run_scheduler_loop(
    args: argparse.Namespace,
    *,
    scheduler_tz: tzinfo,
    now_func: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    now = now_func or (lambda: datetime.now(scheduler_tz))
    executed_keys: set[str] = set()
    coordinator = ScanCoordinator()
    started_runs = 0

    while True:
        current_time = now()
        due_slots, skipped_slots = select_due_slots(
            profile=args.profile,
            now=current_time,
            executed_keys=executed_keys,
            run_missed_window_minutes=args.run_missed_window_minutes,
            catch_up=not args.no_catch_up,
        )
        for skipped in skipped_slots:
            key = scheduled_key(args.profile, skipped.scheduled_time)
            executed_keys.add(key)
            print(
                "scheduled scan skipped: "
                f"scheduled_time={skipped.scheduled_time.isoformat()} "
                f"mode={skipped.slot.mode} reason={skipped.reason}"
            )

        for due in due_slots:
            key = scheduled_key(args.profile, due.scheduled_time)
            if key in executed_keys:
                continue
            started = coordinator.start(
                args=args,
                scheduled_time=due.scheduled_time,
                mode=due.slot.mode,
                sports=due.slot.sports,
                now_func=now,
            )
            executed_keys.add(key)
            if not started:
                print(
                    "scheduled scan skipped: "
                    f"scheduled_time={due.scheduled_time.isoformat()} "
                    f"mode={due.slot.mode} "
                    f"sports={','.join(due.slot.sports)} "
                    "reason=scan_already_running"
                )
                continue
            started_runs += 1
            if args.max_runs is not None and started_runs >= args.max_runs:
                coordinator.join()
                return 0

        sleeper(max(args.sleep_poll_seconds, 0.1))


def select_due_slots(
    *,
    profile: str,
    now: datetime,
    executed_keys: set[str],
    run_missed_window_minutes: float,
    catch_up: bool,
) -> tuple[list[DueSlot], list[SkippedSlot]]:
    due: list[DueSlot] = []
    skipped: list[SkippedSlot] = []
    for slot in get_schedule(profile):
        scheduled_time = now.replace(
            hour=slot.hour,
            minute=slot.minute,
            second=0,
            microsecond=0,
        )
        key = scheduled_key(profile, scheduled_time)
        if key in executed_keys or scheduled_time > now:
            continue
        delay_minutes = (now - scheduled_time).total_seconds() / 60
        if delay_minutes > run_missed_window_minutes:
            skipped.append(
                SkippedSlot(slot, scheduled_time, "missed_window_exceeded")
            )
            continue
        if not catch_up and (now.hour != slot.hour or now.minute != slot.minute):
            skipped.append(SkippedSlot(slot, scheduled_time, "catch_up_disabled"))
            continue
        due.append(DueSlot(slot, scheduled_time))
    return due, skipped


def scheduled_key(profile: str, scheduled_time: datetime) -> str:
    return f"{profile}:{scheduled_time.isoformat()}"


class ScanCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(
        self,
        *,
        args: argparse.Namespace,
        scheduled_time: datetime,
        mode: str,
        sports: tuple[str, ...] | None = None,
        now_func: Callable[[], datetime],
    ) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        thread = threading.Thread(
            target=self._run_and_release,
            kwargs={
                "args": args,
                "scheduled_time": scheduled_time,
                "mode": mode,
                "sports": sports,
                "now_func": now_func,
            },
            daemon=False,
        )
        self._thread = thread
        thread.start()
        return True

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def _run_and_release(
        self,
        *,
        args: argparse.Namespace,
        scheduled_time: datetime,
        mode: str,
        sports: tuple[str, ...] | None,
        now_func: Callable[[], datetime],
    ) -> None:
        try:
            for result in run_scan_slot(
                args=args,
                scheduled_time=scheduled_time,
                mode=mode,
                sports=sports,
                now_func=now_func,
            ):
                print_run_summary(result)
        except Exception as exc:
            print(
                "scheduled scan warning: "
                f"worker_failed scheduled_time={scheduled_time.isoformat()} "
                f"mode={mode} sports={','.join(sports or ())} error={exc}"
            )
        finally:
            self._lock.release()


def run_scan_slot(
    *,
    args: argparse.Namespace,
    scheduled_time: datetime,
    mode: str,
    sports: tuple[str, ...] | None,
    now_func: Callable[[], datetime],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[ScanRunResult]:
    return [
        run_scan_once(
            args=args,
            scheduled_time=scheduled_time,
            mode=mode,
            sport=sport,
            now_func=now_func,
            runner=runner,
        )
        for sport in (sports or (profile_sport(args),))
    ]


def run_scan_once(
    *,
    args: argparse.Namespace,
    scheduled_time: datetime,
    mode: str,
    sport: str | None = None,
    now_func: Callable[[], datetime],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ScanRunResult:
    actual_start_time = now_func()
    resolved_sport = (
        canonical_sport(sport) if sport is not None else profile_sport(args)
    )
    command = build_scan_command(args, mode=mode, sport=resolved_sport)
    log_file = make_log_file_path(
        args.log_dir,
        actual_start_time,
        mode,
        sport=resolved_sport if sport is not None else None,
    )
    env = build_subprocess_env()

    stdout = ""
    stderr = ""
    returncode = 1
    error = ""
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except OSError as exc:
        error = str(exc)
        stderr = error

    fields = parse_scan_output(f"{stdout}\n{stderr}")
    write_run_log(
        log_file=log_file,
        scheduled_time=scheduled_time,
        actual_start_time=actual_start_time,
        mode=mode,
        sport=resolved_sport if sport is not None else "",
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )
    return ScanRunResult(
        scheduled_time=scheduled_time,
        actual_start_time=actual_start_time,
        mode=mode,
        command=command,
        log_file=log_file,
        returncode=returncode,
        fields=fields,
        failed=returncode != 0,
        sport=resolved_sport if sport is not None else "",
        error=error or stderr.strip(),
    )


def make_log_file_path(
    log_dir: Path,
    actual_start_time: datetime,
    mode: str,
    *,
    sport: str | None = None,
) -> Path:
    safe_mode = re.sub(r"[^A-Za-z0-9_-]+", "_", mode).strip("_") or "unknown"
    if sport:
        safe_sport = re.sub(r"[^A-Za-z0-9_-]+", "_", sport).strip("_")
        safe_mode = f"{safe_sport}_{safe_mode}"
    filename = f"scheduled_scan_{actual_start_time:%Y%m%d_%H%M%S}_{safe_mode}.txt"
    return log_dir / filename


def write_run_log(
    *,
    log_file: Path,
    scheduled_time: datetime,
    actual_start_time: datetime,
    mode: str,
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    error: str,
    sport: str = "",
) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    sections = [
        "scheduled scan run",
        f"scheduled_time={scheduled_time.isoformat()}",
        f"actual_start_time={actual_start_time.isoformat()}",
        f"mode={mode}",
        f"sport={sport}",
        f"returncode={returncode}",
        f"command={format_command(command)}",
        "",
        "stdout:",
        stdout.rstrip(),
        "",
        "stderr:",
        stderr.rstrip(),
    ]
    if error:
        sections.extend(["", f"error={error}"])
    log_file.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")


def parse_scan_output(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^([A-Za-z0-9_]+)=(.*)$", line.strip())
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def print_run_summary(result: ScanRunResult) -> None:
    fields = result.fields
    telegram_message_count = fields.get("message_count", "0")
    telegram_sent_count = fields.get(
        "sent_count",
        fields.get("telegram_sent_count", "0"),
    )
    print("scheduled scan summary:")
    print(f"scheduled_time={result.scheduled_time.isoformat()}")
    print(f"actual_start_time={result.actual_start_time.isoformat()}")
    print(f"mode={result.mode}")
    if result.sport:
        print(f"sport={result.sport}")
    print(f"stake_count={fields.get('stake_count', '0')}")
    print(f"polymarket_count={fields.get('polymarket_count', '0')}")
    print(f"matched_count={fields.get('matched_count', '0')}")
    print(f"opportunity_count={fields.get('opportunity_count', '0')}")
    print(
        "oddspapi_total_request_count="
        f"{fields.get('oddspapi_total_request_count', '0')}"
    )
    print(
        "oddspapi_all_keys_exhausted="
        f"{fields.get('oddspapi_all_keys_exhausted', 'false')}"
    )
    print(f"telegram_message_count={telegram_message_count}")
    print(f"telegram_sent_count={telegram_sent_count}")
    print(
        "telegram_dedup_suppressed_count="
        f"{fields.get('telegram_dedup_suppressed_count', '0')}"
    )
    print(
        "telegram_dedup_sent_new_count="
        f"{fields.get('telegram_dedup_sent_new_count', '0')}"
    )
    print(
        "telegram_dedup_state_size="
        f"{fields.get('telegram_dedup_state_size', '0')}"
    )
    print(f"log_file={result.log_file}")
    if result.failed:
        print(
            "scheduled scan warning: "
            f"scan_failed returncode={result.returncode} log_file={result.log_file}"
        )
    oddspapi_total_request_count = _int_field(
        fields.get("oddspapi_total_request_count")
    )
    if oddspapi_total_request_count > DEFAULT_MAX_ODDSPAPI_REQUESTS_PER_RUN:
        print(
            "scheduled scan warning: "
            "oddspapi_total_request_count="
            f"{oddspapi_total_request_count} exceeds "
            f"{DEFAULT_MAX_ODDSPAPI_REQUESTS_PER_RUN}"
        )
    if fields.get("oddspapi_all_keys_exhausted", "").casefold() == "true":
        print("scheduled scan warning: oddspapi_all_keys_exhausted=true")


def _int_field(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def build_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    paths = [path for path in (existing or "").split(os.pathsep) if path]
    if src_dir not in paths:
        paths.insert(0, src_dir)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def format_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def resolve_timezone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        if timezone_name == DEFAULT_TIMEZONE:
            return BerlinFallbackTimezone()
        raise SystemExit(f"unknown timezone: {timezone_name}") from exc


class BerlinFallbackTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=1)
        return timedelta(hours=2 if self._is_dst(dt) else 1)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: datetime | None) -> str:
        return "CEST" if dt is not None and self._is_dst(dt) else "CET"

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        naive_utc = dt.replace(tzinfo=None)
        standard = naive_utc + timedelta(hours=1)
        if self._is_dst_utc(naive_utc):
            return (naive_utc + timedelta(hours=2)).replace(tzinfo=self)
        return standard.replace(tzinfo=self)

    def _is_dst(self, dt: datetime) -> bool:
        naive = dt.replace(tzinfo=None)
        year = naive.year
        start_local = _last_sunday(year, 3).replace(hour=2)
        end_local = _last_sunday(year, 10).replace(hour=3)
        return start_local <= naive < end_local

    def _is_dst_utc(self, naive_utc: datetime) -> bool:
        year = naive_utc.year
        start_utc = _last_sunday(year, 3).replace(hour=1)
        end_utc = _last_sunday(year, 10).replace(hour=1)
        return start_utc <= naive_utc < end_utc


def _last_sunday(year: int, month: int) -> datetime:
    if month == 12:
        day = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = datetime(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


if __name__ == "__main__":
    raise SystemExit(main())
