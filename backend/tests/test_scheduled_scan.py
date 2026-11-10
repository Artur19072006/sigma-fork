from __future__ import annotations

import io
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from provider_odds.cli import scheduled_scan


def _args(*extra: str) -> object:
    return scheduled_scan.build_arg_parser().parse_args(list(extra))


def _result(
    *,
    returncode: int = 0,
    log_file: Path | None = None,
) -> scheduled_scan.ScanRunResult:
    return scheduled_scan.ScanRunResult(
        scheduled_time=datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc),
        actual_start_time=datetime(2026, 6, 30, 6, 0, 1, tzinfo=timezone.utc),
        mode="normal",
        command=["python", "-m", "provider_odds.cli.scan"],
        log_file=log_file or Path("logs/scheduled_scan_20260630_060001_normal.txt"),
        returncode=returncode,
        fields={
            "stake_count": "2",
            "polymarket_count": "3",
            "matched_count": "1",
            "opportunity_count": "1",
            "oddspapi_total_request_count": "4",
            "message_count": "1",
            "sent_count": "0",
        },
        failed=returncode != 0,
        error="failed" if returncode else "",
    )


def _labels_every(
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
    step_minutes: int,
) -> list[str]:
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return [
        f"{total // 60:02d}:{total % 60:02d}"
        for total in range(start, end, step_minutes)
    ]


def _labels_for_sport(
    schedule: tuple[scheduled_scan.ScheduleSlot, ...],
    sport: str,
) -> list[str]:
    return [slot.label for slot in schedule if sport in slot.sports]


class ScheduledScanTest(unittest.TestCase):
    def test_school_day_schedule_contains_cs2_and_lol_without_dota2(self) -> None:
        schedule = scheduled_scan.get_schedule("school_day")
        scheduled_sports = {sport for slot in schedule for sport in slot.sports}

        self.assertEqual(scheduled_sports, {"cs2", "lol"})
        self.assertNotIn("dota2", scheduled_sports)

    def test_school_day_cs2_uses_requested_production_cadence(self) -> None:
        schedule = scheduled_scan.get_schedule("school_day")
        expected = (
            ["06:00", "06:15"]
            + _labels_every(8, 0, 20, 0, 30)
            + _labels_every(20, 0, 23, 0, 8)
        )

        self.assertEqual(_labels_for_sport(schedule, "cs2"), expected)
        self.assertEqual(len(expected), 49)

    def test_school_day_lol_uses_requested_production_cadence(self) -> None:
        schedule = scheduled_scan.get_schedule("school_day")
        expected = (
            ["06:00", "06:15"]
            + _labels_every(8, 0, 20, 0, 90)
            + _labels_every(20, 0, 23, 0, 30)
        )

        self.assertEqual(_labels_for_sport(schedule, "lol"), expected)
        self.assertEqual(len(expected), 16)

    def test_school_day_has_no_night_scans_except_morning_checks(self) -> None:
        schedule = scheduled_scan.get_schedule("school_day")

        for slot in schedule:
            total_minutes = slot.hour * 60 + slot.minute
            self.assertLess(total_minutes, 23 * 60)
            self.assertGreaterEqual(total_minutes, 6 * 60)
            if total_minutes < 8 * 60:
                self.assertIn(slot.label, {"06:00", "06:15"})

    def test_evening_only_schedule_does_not_contain_midnight(self) -> None:
        schedule = scheduled_scan.get_schedule("evening_only")

        self.assertNotIn("00:00", [slot.label for slot in schedule])

    def test_scheduler_defaults_to_europe_berlin_timezone(self) -> None:
        args = _args("--profile", "school_day")

        self.assertEqual(args.timezone, "Europe/Berlin")
        self.assertIn(
            "timezone=Europe/Berlin",
            scheduled_scan.format_schedule("school_day"),
        )

    def test_list_schedule_prints_without_executing_scans(self) -> None:
        with patch(
            "provider_odds.cli.scheduled_scan.run_scan_once"
        ) as run_scan_once:
            with patch(
                "provider_odds.cli.scheduled_scan.run_scheduler_loop"
            ) as run_scheduler_loop:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    result = scheduled_scan.main(
                        ["--profile", "school_day", "--list-schedule"]
                    )

        self.assertEqual(result, 0)
        self.assertIn("06:00 normal sports=cs2,lol", stdout.getvalue())
        self.assertIn("22:56 normal sports=cs2", stdout.getvalue())
        self.assertNotIn("23:00", stdout.getvalue())
        run_scan_once.assert_not_called()
        run_scheduler_loop.assert_not_called()

    def test_once_builds_one_scan_command(self) -> None:
        args = _args("--once")

        command = scheduled_scan.build_scan_command(args, mode="normal")

        self.assertEqual(command.count("-m"), 1)
        self.assertIn("provider_odds.cli.scan", command)
        self.assertIn("--find-arbitrage", command)
        self.assertIn("--telegram-alert", command)

    def test_cache_only_slots_pass_cache_only_resolution_mode(self) -> None:
        args = _args()

        command = scheduled_scan.build_scan_command(args, mode="cache-only")

        mode_index = command.index("--tournament-resolution-mode")
        self.assertEqual(command[mode_index + 1], "cache-only")

    def test_normal_slots_pass_normal_resolution_mode(self) -> None:
        args = _args()

        command = scheduled_scan.build_scan_command(args, mode="normal")

        mode_index = command.index("--tournament-resolution-mode")
        self.assertEqual(command[mode_index + 1], "normal")

    def test_telegram_dry_run_is_passed_only_when_requested(self) -> None:
        without_flag = scheduled_scan.build_scan_command(_args(), mode="normal")
        with_flag = scheduled_scan.build_scan_command(
            _args("--telegram-dry-run"),
            mode="normal",
        )
        lol_with_flag = scheduled_scan.build_scan_command(
            _args("--telegram-dry-run"),
            mode="normal",
            sport="lol",
        )

        self.assertNotIn("--telegram-dry-run", without_flag)
        self.assertIn("--telegram-dry-run", with_flag)
        self.assertIn("--telegram-dry-run", lol_with_flag)

    def test_manual_lol_profile_builds_lol_command_without_real_telegram_by_default(
        self,
    ) -> None:
        args = _args("--profile", "manual_test_lol")

        command = scheduled_scan.build_scan_command(args, mode="normal")

        sport_index = command.index("--sport")
        self.assertEqual(command[sport_index + 1], "lol")
        scope_index = command.index("--lol-tournament-scope")
        self.assertEqual(command[scope_index + 1], "all")
        mapping_index = command.index("--stake-mapping-profile")
        self.assertEqual(
            command[mapping_index + 1],
            scheduled_scan.DEFAULT_LOL_MAPPING_PROFILE,
        )
        self.assertNotIn("--telegram-alert", command)
        self.assertIn("--stake-auto-discover-tournaments", command)
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--polymarket-keyset-tag-id"
            ],
            ["100639", "1"],
        )
        max_pages_index = command.index("--polymarket-keyset-max-pages")
        self.assertEqual(
            command[max_pages_index + 1],
            str(scheduled_scan.DEFAULT_LOL_POLYMARKET_KEYSET_MAX_PAGES),
        )
        empty_pages_index = command.index(
            "--polymarket-keyset-empty-target-pages-stop"
        )
        self.assertIn("--polymarket-keyset-adaptive", command)
        self.assertEqual(
            command[empty_pages_index + 1],
            str(scheduled_scan.DEFAULT_LOL_POLYMARKET_EMPTY_TARGET_PAGES_STOP),
        )

    def test_manual_dota2_profile_builds_dota2_command_with_dry_run_defaults(
        self,
    ) -> None:
        self.assertEqual(scheduled_scan.get_schedule("manual_test_dota2"), ())
        args = _args("--profile", "manual_test_dota2")

        command = scheduled_scan.build_scan_command(args, mode="normal")

        sport_index = command.index("--sport")
        self.assertEqual(command[sport_index + 1], "dota2")
        mapping_index = command.index("--stake-mapping-profile")
        self.assertEqual(
            command[mapping_index + 1],
            scheduled_scan.DEFAULT_DOTA2_MAPPING_PROFILE,
        )
        self.assertIn("--stake-auto-discover-tournaments", command)
        self.assertIn("--find-arbitrage", command)
        fee_index = command.index("--polymarket-fee-rate")
        self.assertEqual(command[fee_index + 1], "0.03")
        self.assertIn("--debug", command)
        self.assertIn("--polymarket-keyset-adaptive", command)
        self.assertIn("--polymarket-page-preseed-dota2-defaults", command)
        self.assertNotIn("--polymarket-preseed-sport-defaults", command)
        empty_pages_index = command.index(
            "--polymarket-keyset-empty-target-pages-stop"
        )
        self.assertEqual(
            command[empty_pages_index + 1],
            str(scheduled_scan.DEFAULT_DOTA2_POLYMARKET_EMPTY_TARGET_PAGES_STOP),
        )
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--polymarket-keyset-tag-id"
            ],
            ["100639", "1"],
        )
        self.assertNotIn("--telegram-alert", command)
        self.assertIn("--telegram-dry-run", command)
        self.assertNotIn("38029", command)
        self.assertNotIn("47329", command)

    def test_manual_lol_and_dota2_profiles_remain_available(self) -> None:
        self.assertEqual(scheduled_scan.get_schedule("manual_test_lol"), ())
        self.assertEqual(scheduled_scan.get_schedule("manual_test_dota2"), ())

    def test_manual_lol_profile_allows_msi_scope_and_keyset_tag_override(self) -> None:
        args = _args(
            "--profile",
            "manual_test_lol",
            "--lol-tournament-scope",
            "msi",
            "--polymarket-keyset-tag-id",
            "12345,67890",
        )

        command = scheduled_scan.build_scan_command(args, mode="normal")

        scope_index = command.index("--lol-tournament-scope")
        self.assertEqual(command[scope_index + 1], "msi")
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--polymarket-keyset-tag-id"
            ],
            ["12345", "67890"],
        )

    def test_manual_lol_profile_allows_dry_run_or_explicit_telegram_alert(
        self,
    ) -> None:
        dry_run_command = scheduled_scan.build_scan_command(
            _args("--profile", "manual_test_lol", "--telegram-dry-run"),
            mode="normal",
        )
        alert_command = scheduled_scan.build_scan_command(
            _args("--profile", "manual_test_lol", "--telegram-alert"),
            mode="normal",
        )

        self.assertIn("--telegram-dry-run", dry_run_command)
        self.assertNotIn("--telegram-alert", dry_run_command)
        self.assertNotIn("--polymarket-preseed-sport-defaults", dry_run_command)
        self.assertNotIn("--polymarket-page-preseed-dota2-defaults", dry_run_command)
        self.assertIn("--telegram-alert", alert_command)

    def test_lol_production_command_uses_lol_mapping_profile(self) -> None:
        command = scheduled_scan.build_scan_command(
            _args("--profile", "school_day"),
            mode="normal",
            sport="lol",
        )

        sport_index = command.index("--sport")
        mapping_index = command.index("--stake-mapping-profile")
        self.assertEqual(command[sport_index + 1], "lol")
        self.assertEqual(
            command[mapping_index + 1],
            scheduled_scan.DEFAULT_LOL_MAPPING_PROFILE,
        )
        self.assertIn("--telegram-alert", command)
        self.assertNotIn("--telegram-dry-run", command)

    def test_lol_is_not_enabled_in_other_scheduled_profiles(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as cm:
                scheduled_scan.main(
                    ["--profile", "evening_only", "--sport", "lol", "--dry-run"]
                )

        self.assertEqual(cm.exception.code, 2)
        self.assertIn(
            "LoL scans are only allowed with --profile school_day",
            stderr.getvalue(),
        )

    def test_dota2_sport_is_not_enabled_in_scheduled_profiles(self) -> None:
        for profile in ("school_day", "evening_only", "manual_test"):
            with self.subTest(profile=profile):
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as cm:
                        scheduled_scan.main(
                            ["--profile", profile, "--sport", "dota2", "--dry-run"]
                        )

                self.assertEqual(cm.exception.code, 2)
                self.assertIn(
                    "Dota2 scans are only allowed with --profile manual_test_dota2",
                    stderr.getvalue(),
                )

    def test_live_profiles_still_default_to_cs2_without_dota2_mapping(self) -> None:
        for profile in ("school_day", "evening_only"):
            with self.subTest(profile=profile):
                command = scheduled_scan.build_scan_command(
                    _args("--profile", profile),
                    mode="normal",
                )

                sport_index = command.index("--sport")
                self.assertEqual(command[sport_index + 1], "cs2")
                self.assertNotIn("dota2", command)
                self.assertNotIn(
                    scheduled_scan.DEFAULT_DOTA2_MAPPING_PROFILE,
                    command,
                )
                self.assertNotIn("--polymarket-preseed-sport-defaults", command)
                self.assertNotIn("--polymarket-page-preseed-dota2-defaults", command)

    def test_shared_slot_runs_cs2_then_lol(self) -> None:
        slot = next(
            slot
            for slot in scheduled_scan.get_schedule("school_day")
            if slot.label == "06:00"
        )
        with patch(
            "provider_odds.cli.scheduled_scan.run_scan_once",
            return_value=_result(),
        ) as run_scan_once:
            results = scheduled_scan.run_scan_slot(
                args=_args("--profile", "school_day"),
                scheduled_time=datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc),
                mode=slot.mode,
                sports=slot.sports,
                now_func=lambda: datetime(
                    2026,
                    6,
                    30,
                    6,
                    0,
                    tzinfo=timezone.utc,
                ),
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(
            [call.kwargs["sport"] for call in run_scan_once.call_args_list],
            ["cs2", "lol"],
        )

    def test_live_profiles_keep_cs2_mapping_profile(self) -> None:
        command = scheduled_scan.build_scan_command(
            _args("--profile", "school_day"),
            mode="normal",
        )

        mapping_index = command.index("--stake-mapping-profile")
        self.assertEqual(
            command[mapping_index + 1],
            scheduled_scan.DEFAULT_MAPPING_PROFILE,
        )
        self.assertNotIn(scheduled_scan.DEFAULT_LOL_MAPPING_PROFILE, command)
        self.assertNotIn("--polymarket-keyset-adaptive", command)
        self.assertNotIn("--polymarket-preseed-sport-defaults", command)
        self.assertNotIn("--polymarket-page-preseed-dota2-defaults", command)

    def test_log_filename_includes_timestamp_and_mode(self) -> None:
        log_file = scheduled_scan.make_log_file_path(
            Path("logs"),
            datetime(2026, 6, 30, 21, 3, 5, tzinfo=timezone.utc),
            "cache-only",
        )

        self.assertEqual(
            log_file,
            Path("logs/scheduled_scan_20260630_210305_cache-only.txt"),
        )

    def test_failed_scan_does_not_crash_scheduler_loop(self) -> None:
        args = _args("--max-runs", "1")
        now = datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc)

        with patch(
            "provider_odds.cli.scheduled_scan.run_scan_once",
            return_value=_result(returncode=2),
        ):
            with patch("provider_odds.cli.scheduled_scan.print_run_summary"):
                result = scheduled_scan.run_scheduler_loop(
                    args,
                    scheduler_tz=timezone.utc,
                    now_func=lambda: now,
                    sleeper=lambda seconds: None,
                )

        self.assertEqual(result, 0)

    def test_no_overlapping_scans_skips_second_run(self) -> None:
        args = _args()
        coordinator = scheduled_scan.ScanCoordinator()
        started = threading.Event()
        release = threading.Event()

        def fake_run_scan_once(**kwargs: object) -> scheduled_scan.ScanRunResult:
            started.set()
            release.wait(timeout=5)
            return _result()

        with patch(
            "provider_odds.cli.scheduled_scan.run_scan_once",
            side_effect=fake_run_scan_once,
        ):
            with patch("provider_odds.cli.scheduled_scan.print_run_summary"):
                first_started = coordinator.start(
                    args=args,
                    scheduled_time=datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc),
                    mode="normal",
                    now_func=lambda: datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc),
                )
                self.assertTrue(first_started)
                self.assertTrue(started.wait(timeout=5))

                second_started = coordinator.start(
                    args=args,
                    scheduled_time=datetime(2026, 6, 30, 6, 30, tzinfo=timezone.utc),
                    mode="cache-only",
                    now_func=lambda: datetime(
                        2026,
                        6,
                        30,
                        6,
                        30,
                        tzinfo=timezone.utc,
                    ),
                )
                release.set()
                coordinator.join()

        self.assertFalse(second_started)

    def test_missed_runs_older_than_window_are_skipped_with_no_catch_up(self) -> None:
        now = datetime(2026, 6, 30, 22, 6, tzinfo=timezone.utc)

        due, skipped = scheduled_scan.select_due_slots(
            profile="evening_only",
            now=now,
            executed_keys=set(),
            run_missed_window_minutes=5,
            catch_up=False,
        )

        self.assertEqual(due, [])
        skipped_by_label = {item.slot.label: item.reason for item in skipped}
        self.assertEqual(
            skipped_by_label["22:00"],
            "missed_window_exceeded",
        )

    def test_run_scan_once_writes_failed_log_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = _args("--log-dir", tmp)

            def fake_runner(*run_args: object, **kwargs: object) -> object:
                return subprocess.CompletedProcess(
                    run_args[0],
                    2,
                    stdout="scan summary:\nstake_count=0\n",
                    stderr="boom",
                )

            result = scheduled_scan.run_scan_once(
                args=args,
                scheduled_time=datetime(2026, 6, 30, 6, 0, tzinfo=timezone.utc),
                mode="normal",
                now_func=lambda: datetime(
                    2026,
                    6,
                    30,
                    6,
                    0,
                    1,
                    tzinfo=timezone.utc,
                ),
                runner=fake_runner,
            )

            self.assertTrue(result.failed)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.log_file.exists())
            self.assertIn("boom", result.log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
