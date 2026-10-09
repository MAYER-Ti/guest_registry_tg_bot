from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import unittest

from activity import MOSCOW, active_guests, calculate_stats
from reporting import ReportGuest, ReportSnapshot
from storage import Guest, Visit, VisitSummary


UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value)


def visit(id, started, stopped=None, guest_id=1):
    return Visit(id, guest_id, started, stopped, 101, 102 if stopped else None)


def row(*visits, guest_id=1, source=1, name="Гость"):
    timestamp = "2026-01-01T00:00:00+00:00"
    guest = Guest(guest_id, name, "+79990000001", "79990000001", "", b"", "photo",
                  101, 101, timestamp, timestamp, 1)
    visits = tuple(replace(item, guest_id=guest_id) for item in visits)
    active = next((item for item in visits if item.stopped_at is None), None)
    summary = VisitSummary(active, sum(item.stopped_at is not None for item in visits), 0, 0)
    return ReportGuest(source, "Основная база" if source == 1 else f"База {source}",
                       guest, summary, tuple(visits))


def snapshot(now="2026-10-09T09:00:00+00:00", *rows):
    return ReportSnapshot(instant(now), ("Основная база", "База 2"), tuple(rows))


class ActivityTests(unittest.TestCase):
    def test_empty_snapshot_all_periods_and_immutable_result(self):
        for period, label in (("day", "За день"), ("week", "За неделю"), ("month", "За месяц")):
            result = calculate_stats(snapshot(), period)
            self.assertEqual((result.period, result.label), (period, label))
            self.assertEqual((result.visit_count, result.unique_guests,
                              result.total_seconds, result.overlap_guests), (0, 0, 0, 0))
            self.assertEqual(result.top, ())
            self.assertEqual(result.end, instant("2026-10-09T09:00:00+00:00"))
            self.assertEqual(result.start.utcoffset(), timedelta(0))
            with self.assertRaises(FrozenInstanceError):
                result.visit_count = 1
        self.assertEqual(active_guests(snapshot()), ())

    def test_day_boundary_uses_moscow_date_after_utc_evening(self):
        result = calculate_stats(snapshot("2026-10-08T22:00:00+00:00"))
        self.assertEqual(result.start, instant("2026-10-08T21:00:00+00:00"))
        self.assertEqual(result.start.astimezone(MOSCOW).day, 9)

    def test_exact_midnight_has_empty_elapsed_window(self):
        now = "2026-10-08T21:00:00+00:00"
        old = visit(1, "2026-10-08T20:00:00+00:00")
        arrival = visit(2, now, now)
        result = calculate_stats(snapshot(now, row(old), row(arrival, guest_id=2)))
        self.assertEqual(result.start, result.end)
        self.assertEqual((result.visit_count, result.unique_guests, result.total_seconds), (1, 1, 0))
        self.assertEqual(result.overlap_guests, 0)

    def test_week_is_calendar_monday_not_rolling_seven_days(self):
        result = calculate_stats(snapshot("2026-10-11T20:59:59+00:00"), "week")
        self.assertEqual(result.start, instant("2026-10-04T21:00:00+00:00"))
        monday = calculate_stats(snapshot("2026-10-11T21:00:00+00:00"), "week")
        self.assertEqual(monday.start, monday.end)

    def test_week_crosses_year_boundary(self):
        result = calculate_stats(snapshot("2027-01-01T09:00:00+00:00"), "week")
        self.assertEqual(result.start, instant("2026-12-27T21:00:00+00:00"))

    def test_month_boundary_leap_day_and_new_year(self):
        leap = calculate_stats(snapshot("2028-02-29T20:59:59+00:00"), "month")
        self.assertEqual(leap.start, instant("2028-01-31T21:00:00+00:00"))
        march = calculate_stats(snapshot("2028-02-29T21:00:00+00:00"), "month")
        self.assertEqual(march.start, march.end)
        january = calculate_stats(snapshot("2026-12-31T21:00:00+00:00"), "month")
        self.assertEqual(january.start, january.end)

    def test_arrivals_and_elapsed_overlap_have_separate_definitions(self):
        guest = row(
            visit(1, "2026-10-08T20:00:00+00:00", "2026-10-08T22:00:00+00:00"),
            visit(2, "2026-10-09T00:00:00+00:00", "2026-10-09T02:00:00+00:00"),
            visit(3, "2026-10-09T09:00:00+00:00", "2026-10-09T09:00:00+00:00"))
        earlier_active = row(visit(4, "2026-10-08T20:00:00+00:00", guest_id=2), guest_id=2)
        future = row(visit(5, "2026-10-09T10:00:00+00:00", guest_id=3), guest_id=3)
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", guest, earlier_active, future))
        self.assertEqual((result.visit_count, result.unique_guests), (2, 1))
        self.assertEqual(result.total_seconds, 15 * 3600)
        self.assertEqual(result.overlap_guests, 2)
        self.assertEqual(len(result.top), 1)
        self.assertEqual((result.top[0].row, result.top[0].visit_count,
                          result.top[0].total_seconds), (guest, 2, 3 * 3600))

    def test_visits_ending_at_start_do_not_add_elapsed_or_arrivals(self):
        ended = row(visit(1, "2026-10-08T20:00:00+00:00", "2026-10-08T21:00:00+00:00"))
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", ended))
        self.assertEqual((result.visit_count, result.unique_guests,
                          result.total_seconds, result.overlap_guests), (0, 0, 0, 0))

    def test_zero_length_arrivals_count_without_adding_time(self):
        guest = row(visit(1, "2026-10-08T21:00:00+00:00", "2026-10-08T21:00:00+00:00"),
                    visit(2, "2026-10-09T09:00:00+00:00", "2026-10-09T09:00:00+00:00"))
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", guest))
        self.assertEqual((result.visit_count, result.unique_guests, result.total_seconds), (2, 1, 0))
        self.assertEqual(result.top[0].visit_count, 2)

    def test_future_visits_excluded_and_future_stop_clipped_at_now(self):
        future = row(visit(1, "2026-10-10T00:00:00+00:00", "2026-10-10T02:00:00+00:00"))
        clipped = row(visit(2, "2026-10-09T08:00:00+00:00", "2026-10-10T02:00:00+00:00",
                            guest_id=2), guest_id=2)
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", future, clipped))
        self.assertEqual((result.visit_count, result.unique_guests, result.total_seconds), (1, 1, 3600))

    def test_sources_with_colliding_card_and_visit_ids_keep_distinct_guests(self):
        first = row(visit(1, "2026-10-09T06:00:00+00:00", "2026-10-09T07:00:00+00:00"))
        second = row(visit(1, "2026-10-09T06:00:00+00:00", "2026-10-09T08:00:00+00:00"), source=2)
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", first, second))
        self.assertEqual((result.visit_count, result.unique_guests, result.total_seconds), (2, 2, 10800))
        self.assertEqual([item.row.source_index for item in result.top], [2, 1])

    def test_ranking_prefers_visits_then_period_time_then_stable_name_and_identity(self):
        twice = row(visit(1, "2026-10-09T07:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                    visit(2, "2026-10-09T08:00:00+00:00", "2026-10-09T08:00:00+00:00"),
                    guest_id=9, name="Яков")
        long = row(visit(3, "2026-10-09T05:00:00+00:00", "2026-10-09T08:00:00+00:00"),
                   guest_id=8, name="Яна")
        a1 = row(visit(4, "2026-10-09T06:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                 guest_id=1, name="Анна")
        a2 = row(visit(5, "2026-10-09T06:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                 guest_id=2, name="анна")
        a3 = row(visit(6, "2026-10-09T06:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                 guest_id=1, source=2, name="АННА")
        b = row(visit(7, "2026-10-09T06:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                guest_id=3, name="Борис")
        rows = (b, a3, a2, long, a1, twice)
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", *rows))
        self.assertEqual([item.row for item in result.top], [twice, long, a1, a2, a3])
        reversed_result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", *reversed(rows)))
        self.assertEqual(reversed_result.top, result.top)
        self.assertEqual(result.unique_guests, 6)

    def test_ranking_duration_includes_earlier_visits_for_guests_with_arrivals(self):
        earlier = row(visit(1, "2026-10-08T20:00:00+00:00", "2026-10-09T05:00:00+00:00"),
                      visit(2, "2026-10-09T08:00:00+00:00", "2026-10-09T08:00:00+00:00"),
                      name="Яков")
        other = row(visit(3, "2026-10-09T05:00:00+00:00", "2026-10-09T07:00:00+00:00"),
                    guest_id=2, name="Анна")
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", other, earlier))
        self.assertEqual(result.top[0].row, earlier)
        self.assertEqual(result.top[0].total_seconds, 8 * 3600)

    def test_offset_aware_timestamps_compare_as_utc_instants(self):
        # A change in the timestamp's UTC offset must not lose elapsed time.
        guest = row(visit(1, "2026-10-09T07:00:00+04:00", "2026-10-09T07:00:00+03:00"))
        result = calculate_stats(snapshot("2026-10-09T09:00:00+03:00", guest))
        self.assertEqual(result.end, instant("2026-10-09T06:00:00+00:00"))
        self.assertEqual(result.total_seconds, 3600)

    def test_fractional_elapsed_matches_whole_second_visit_summary(self):
        guest = row(visit(1, "2026-10-09T08:00:00.100000+00:00", "2026-10-09T08:00:01.900000+00:00"),
                    visit(2, "2026-10-09T08:00:02.100000+00:00", "2026-10-09T08:00:02.900000+00:00"))
        result = calculate_stats(snapshot("2026-10-09T09:00:00+00:00", guest))
        self.assertEqual((result.visit_count, result.total_seconds, result.overlap_guests), (2, 1, 1))

    def test_active_guests_oldest_first_stable_ties_and_future_start_excluded(self):
        future = row(visit(1, "2026-10-10T00:00:00+00:00"), guest_id=8)
        stopped = row(visit(2, "2026-10-08T00:00:00+00:00", "2026-10-09T02:00:00+00:00"), guest_id=9)
        newest = row(visit(3, "2026-10-09T07:00:00+00:00"), guest_id=3)
        same_source_two = row(visit(4, "2026-10-09T06:00:00+00:00"), source=2)
        same_id_two = row(visit(5, "2026-10-09T06:00:00+00:00"), guest_id=2)
        same_id_one = row(visit(6, "2026-10-09T06:00:00+00:00"))
        oldest = row(visit(7, "2026-10-07T00:00:00+00:00"), guest_id=4)
        data = snapshot("2026-10-09T09:00:00+00:00", future, newest, stopped, same_source_two,
                        same_id_two, same_id_one, oldest)
        self.assertEqual(active_guests(data), (oldest, same_id_one, same_id_two, same_source_two, newest))

    def test_active_guest_started_exactly_now_is_visible(self):
        guest = row(visit(1, "2026-10-09T12:00:00+03:00"))
        self.assertEqual(active_guests(snapshot("2026-10-09T09:00:00+00:00", guest)), (guest,))

    def test_calculations_never_modify_snapshot_or_rows(self):
        guest = row(visit(1, "2026-10-08T00:00:00+00:00"))
        data = snapshot("2026-10-09T09:00:00+00:00", guest)
        before = repr(data)
        for period in ("day", "week", "month"):
            calculate_stats(data, period)
        active_guests(data)
        self.assertEqual(repr(data), before)
        self.assertIs(data.rows[0], guest)

    def test_invalid_period_and_naive_report_instant_rejected(self):
        with self.assertRaises(ValueError):
            calculate_stats(snapshot(), "year")
        naive = ReportSnapshot(datetime(2026, 10, 9), (), ())
        with self.assertRaises(ValueError):
            calculate_stats(naive)
        with self.assertRaises(ValueError):
            active_guests(naive)


if __name__ == "__main__":
    unittest.main()
