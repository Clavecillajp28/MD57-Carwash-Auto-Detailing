"""Read-only live estimates. Saved arrival predictions remain audit evidence.

Queue delay is resource availability, not the wash itself. Service durations
use recent recorded jobs for the same service and size, then a zero-queue AI
estimate, then the catalogue standard. None means availability is unknown.
"""
import math
from collections import defaultdict
from datetime import timedelta
from shop_clock import datetime
from statistics import median

from timing_policy import GAP_MINUTES, OVERDUE_ALLOWANCE
from utils import BUSINESS_OPEN_HOUR, BUSINESS_CLOSE_HOUR, MAX_PLAUSIBLE_SERVICE_MINUTES, parse_booking_datetime

STAMP = '%Y-%m-%d %H:%M:%S'
MIN_HISTORY = 5
HISTORY_LIMIT = 100


def _stamp(value):
    try:
        return datetime.strptime(value, STAMP)
    except (TypeError, ValueError):
        return None


def _first_gap(intervals, start, minutes):
    for busy_start, busy_end in sorted(intervals, key=lambda pair: pair[0]):
        if busy_end is not None and busy_end <= start:
            continue
        if start + timedelta(minutes=minutes) <= busy_start:
            return start
        if busy_end is None:
            return None
        start = max(start, busy_end)
    return start


class ServiceEstimates:
    def __init__(self, cursor, predictor, rain_mm=0, now=None):
        self.cursor, self.predictor, self.rain_mm = cursor, predictor, rain_mm
        self.now = (now or datetime.now()).replace(microsecond=0)
        self.jobs = [dict(row) for row in cursor.execute("""
            SELECT r.reservation_id AS id, r.service_id, r.vehicle_type, r.assigned_bay,
                   r.queue_status AS status, r.queue_number, r.booking_date, r.booking_time,
                   r.started_at, r.assigned_staff_id,
                   s.base_duration_minutes AS base_minutes,
                   s.base_duration_minutes + COALESCE(v.multiplier_minutes, 0) AS duration_minutes
            FROM reservations r JOIN services s ON s.service_id=r.service_id
            LEFT JOIN vehicle_types v ON v.type_name=r.vehicle_type
            WHERE r.queue_status IN ('waiting','in_progress','pending_confirmation')
        """).fetchall()]
        self.on_duty = {r[0] for r in cursor.execute("""
            SELECT a.staff_user_id FROM staff_attendance a JOIN users u ON u.user_id=a.staff_user_id
            WHERE a.date_str=? AND a.is_synthetic=0 AND u.role='staff' AND u.disabled_at IS NULL
              AND a.status IN ('Present','Late') AND a.time_out='--:--'
              AND a.attendance_id=(SELECT MAX(b.attendance_id) FROM staff_attendance b
                  WHERE b.staff_user_id=a.staff_user_id AND b.date_str=a.date_str AND b.is_synthetic=0)
        """, (self.now.strftime('%Y-%m-%d'),)).fetchall()}
        self.durations, self.days = {}, {}

    def _pool(self, day):
        if day == self.now.date() and self.on_duty:
            return self.on_duty, False
        rows = self.cursor.execute("""
            SELECT DISTINCT s.staff_user_id FROM staff_shifts s JOIN users u ON u.user_id=s.staff_user_id
            WHERE s.shift_date=? AND u.role='staff' AND u.disabled_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM staff_unavailability a
                  WHERE a.staff_user_id=s.staff_user_id AND a.unavailable_date=s.shift_date)
        """, (day.isoformat(),)).fetchall()
        return {r[0] for r in rows}, True

    def _duration(self, job):
        key = (job['service_id'], job['vehicle_type'])
        if key not in self.durations:
            rows = self.cursor.execute("""
                SELECT started_at, completed_at FROM (
                    SELECT service_id,vehicle_type,queue_status,started_at,completed_at FROM reservations WHERE is_synthetic=0
                    UNION ALL
                    SELECT service_id,vehicle_type,queue_status,started_at,completed_at FROM reservations_archive WHERE is_synthetic=0
                ) WHERE service_id=? AND vehicle_type=? AND queue_status IN ('ready','completed')
                  AND completed_at<=? AND started_at IS NOT NULL
                ORDER BY completed_at DESC LIMIT ?
            """, (*key, self.now.strftime(STAMP), HISTORY_LIMIT)).fetchall()
            actual = []
            for row in rows:
                start, finish = _stamp(row['started_at']), _stamp(row['completed_at'])
                minutes = (finish-start).total_seconds()/60 if start and finish else 0
                if 0 < minutes <= MAX_PLAUSIBLE_SERVICE_MINUTES:
                    actual.append(minutes)
            if len(actual) >= MIN_HISTORY:
                duration, source = median(actual), 'recorded_history'
            else:
                duration, source = job['duration_minutes'], 'service_standard'
                try:
                    predicted = float(self.predictor.predict_ewt(
                        0, len(self.on_duty), self.rain_mm, job['base_minutes'],
                        work_ahead_minutes=0, hour=self.now.hour))
                    predicted += max(0, job['duration_minutes'] - job['base_minutes'])
                    if math.isfinite(predicted) and 0 < predicted <= MAX_PLAUSIBLE_SERVICE_MINUTES:
                        duration, source = predicted, 'ai'
                except Exception:
                    # A failed prediction must not hide the catalogue duration.
                    pass
            self.durations[key] = (max(1, math.ceil(duration)), source, len(actual))
        return self.durations[key]

    def _result(self, job, status, message, start=None, finish=None, reference=None):
        duration, source, samples = self._duration(job)
        reference = reference or self.now
        return dict(status=status, message=message, service_minutes=duration,
                    queue_wait_minutes=max(0, math.ceil((start-reference).total_seconds()/60)) if start else None,
                    total_minutes=max(0, math.ceil((finish-reference).total_seconds()/60)) if finish else None,
                    estimated_start_at=start.strftime(STAMP) if start else None,
                    estimated_finish_at=finish.strftime(STAMP) if finish else None,
                    duration_source=source, history_samples=samples, as_of=self.now.strftime(STAMP))

    def _day(self, job):
        booked = parse_booking_datetime(job.get('booking_date'), job.get('booking_time'))
        return max(self.now.date(), booked.date()) if booked else self.now.date()

    def _schedule(self, day, extra=None):
        opening = datetime.combine(day, datetime.min.time()).replace(hour=BUSINESS_OPEN_HOUR)
        closing = opening.replace(hour=BUSINESS_CLOSE_HOUR)
        earliest = max(opening, self.now)
        pool, provisional = self._pool(day)
        bays, staff = defaultdict(list), {uid: [] for uid in pool}
        results = {}
        pending_bays = set()
        if day == self.now.date():
            for job in self.jobs:
                if job['status'] not in ('in_progress', 'pending_confirmation'):
                    continue
                start = _stamp(job['started_at'])
                duration = self._duration(job)[0]
                if job['status'] == 'pending_confirmation':
                    finish, status = None, 'awaiting_confirmation'
                    message = 'Wash finished; awaiting final confirmation.'
                    pending_bays.add(job['assigned_bay'])
                else:
                    finish = (start or self.now) + timedelta(minutes=duration)
                    overdue = finish <= self.now
                    finish = max(finish, self.now + timedelta(minutes=OVERDUE_ALLOWANCE))
                    status = 'overdue' if overdue else 'in_progress'
                    message = 'Taking longer than estimated; finish time may change.' if overdue else 'Service in progress.'
                results[job['id']] = self._result(job, status, message, start, finish)
                bays[job['assigned_bay']].append((self.now, finish + timedelta(minutes=GAP_MINUTES) if finish else None))
                if job['assigned_staff_id'] in staff:
                    staff[job['assigned_staff_id']].append((self.now, finish))

        waiting = [j for j in self.jobs if j['status'] == 'waiting' and self._day(j) == day]
        if extra:
            waiting.append(extra)
        def order(job):
            booked = parse_booking_datetime(job.get('booking_date'), job.get('booking_time'))
            return (0 if booked else 1, booked or earliest, job.get('queue_number') or 0, job['id'])
        # Reserve booked slots first, then fit walk-ins into the remaining gaps.
        for job in sorted(waiting, key=order):
            booked = parse_booking_datetime(job.get('booking_date'), job.get('booking_time'))
            reference = max(self.now, booked) if booked else self.now
            start_at = max(earliest, booked or earliest)
            if earliest >= closing:
                results[job['id']] = self._result(job, 'closed', 'Shop closed. Choose a time during opening hours.')
                continue
            if not pool or (provisional and day == self.now.date() and self.now >= opening and not (booked and booked > self.now)):
                results[job['id']] = self._result(job, 'awaiting_staff', 'Awaiting staff availability.')
                continue
            duration = self._duration(job)[0]
            options = []
            for uid, calendar in staff.items():
                start = start_at
                while start is not None:
                    bay_start = _first_gap(bays[job['assigned_bay']], start, duration + GAP_MINUTES)
                    staff_start = _first_gap(calendar, bay_start, duration) if bay_start else None
                    if staff_start is None or staff_start == start:
                        start = staff_start
                        break
                    start = staff_start
                if start is not None:
                    options.append((start, uid))
            if not options:
                reason = 'Bay awaiting final confirmation.' if job['assigned_bay'] in pending_bays else 'Awaiting staff availability or completion confirmation.'
                results[job['id']] = self._result(job, 'awaiting_confirmation', reason)
                continue
            start, uid = min(options)
            finish = start + timedelta(minutes=duration)
            if finish > closing:
                results[job['id']] = self._result(job, 'closed', 'No time to finish before closing. Choose another slot.')
                continue
            bays[job['assigned_bay']].append((start, finish + timedelta(minutes=GAP_MINUTES)))
            staff[uid].append((start, finish))
            message = 'Based on scheduled staff; availability will be confirmed on arrival.' if provisional else 'Estimate updates with the queue and staff availability.'
            results[job['id']] = self._result(job, 'scheduled' if provisional else 'available', message, start, finish, reference)
        return results

    def for_job(self, reservation_id):
        job = next((j for j in self.jobs if j['id'] == reservation_id), None)
        if job is None:
            return None
        day = self._day(job) if job['status'] == 'waiting' else self.now.date()
        if day not in self.days:
            self.days[day] = self._schedule(day)
        return self.days[day].get(reservation_id)

    def preview(self, service, vehicle_type, standard_minutes, booking_date=None, booking_time=None):
        job = dict(id=-1, service_id=service['service_id'], vehicle_type=vehicle_type,
                   assigned_bay=service['default_bay'], base_minutes=service['base_duration_minutes'],
                   duration_minutes=standard_minutes, status='waiting', queue_number=2**63-1,
                   booking_date=booking_date, booking_time=booking_time)
        return self._schedule(self._day(job), extra=job)[-1]
