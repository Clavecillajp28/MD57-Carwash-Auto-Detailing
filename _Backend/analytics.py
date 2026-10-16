"""Manager Analytics - every figure on the Analytics page and in its PDFs.

Everything is computed for one period (whole days, start..end inclusive),
so the numbers on the page and in every PDF agree with each other. The
definitions:

- served: a finished service ('ready' or 'completed'), counted on the day
  it was finished. Cancelled and no-show bookings are never served.
- arrival: a walk-in's is when it was entered; a booked customer's is
  their check-in time, or their booked time if nobody checked them in.
- queue wait: arrival to start of service. A booked customer who came
  early is measured from their booked time - waiting for their own slot
  isn't the shop's delay.
- service time: start to finish.
- visit / returning vehicle: vehicles.py's definition (a plate on a day).

Sales are deliberately absent: payment recording is outside this system's
scope, so no peso amount is reported anywhere here.
"""
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from database import get_db_connection
from data_scope import SOURCES, parse_source, source_sql
from extensions import mcda_engine
from settings import get_setting
from utils import MAX_PLAUSIBLE_SERVICE_MINUTES
from vehicles import plate_key

STAMP = '%Y-%m-%d %H:%M:%S'
WALK_IN = 'Walk-in Customer'
DONE = ('ready', 'completed')
HOURS = list(range(8, 21))  # 8 AM - 8 PM arrivals (the shop closes at 9)
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
PERIOD_PRESETS = (7, 30, 90)
MAX_PERIOD_DAYS = 366
# Fewer cars/reviews than this behind a figure is too few to read much into -
# the page fades it or says "not enough data" instead.
MIN_SAMPLE = 5
# How many staff work on one car at a time is a shop setting (settings.py,
# changed on the Analytics page), not something the system can measure - a
# job records only the staff member assigned to it.
MIN_STAFF_ON_DUTY = 2
MAX_ACCURACY_POINTS = 400    # newest walk-ins plotted on the accuracy scatter
RETURN_CURVE_DAYS = 60
CANCEL_REASONS = {
    'no_show': 'No-show',
    'customer': 'Cancelled by customer',
    'staff': 'Cancelled by staff (incl. walk-ins who left)',
    'staff_absent': 'No staff on duty',
}
# The page's sections and charts, in order - one list for the page and the PDFs.
SECTIONS = {
    'demand': {'title': 'Demand', 'blurb': 'When customers come, and how many staff that calls for.',
               'blocks': ['daily', 'heatmap', 'weather', 'staffing']},
    'operations': {'title': 'Operations', 'blurb': "How long customers wait, how long jobs take, and what didn't go ahead.",
                   'blocks': ['wait', 'service_times', 'cancellations', 'accuracy']},
    'customers': {'title': 'Services & Customers', 'blurb': 'What customers have done, what they drive, and how often they come back.',
                  'blocks': ['services', 'models', 'returns']},
    'staff': {'title': 'Staff', 'blurb': 'Workload, pace, punctuality, customer ratings and the MCDA ranking.',
              'blocks': ['staff']},
}
BLOCKS = {
    'daily': ('Cars served per day',
              "Finished services per calendar day, with the average of the last 7 days. A day's weather is the condition "
              'recorded on most of the bookings made that day.'),
    'heatmap': ('Busy hours',
                'Arrival time of every finished service, walk-ins included, averaged over the number of times that weekday '
                'occurred in the period. Booked customers count at their check-in time. Days still in progress are left out.'),
    'weather': ('Weather effect',
                'Average cars per day for each kind of day, so a common kind of day does not win just by happening more often. '
                'Weather is recorded from OpenWeatherMap when a booking is made.'),
    'staffing': ('Recommended staffing',
                 "Busiest hour of that weekday (average cars arriving) × average service time ({avg_service} min) ÷ 60 = cars being "
                 "worked on at once, at most {bays} bays; × {crew} staff per car = staff needed (minimum {minimum}). Compared with the "
                 "average number of staff who clocked in on that weekday."),
    'wait': ('Queue wait by arrival time',
             'Walk-ins: from arrival to the start of service. Booked customers: from their booked time (or check-in, if later) to '
             "the start - time spent early, before their slot, isn't counted against the shop. Hours with fewer than "
             f'{MIN_SAMPLE} cars are faded.'),
    'service_times': ('Service time vs standard',
                      "Start to finish, as recorded by staff. The standard is the service's set duration plus the extra minutes "
                      'for that vehicle size.'),
    'cancellations': ("Bookings that didn't go ahead",
                      'Cancellations in the period by reason. A no-show is a booked customer released 30 minutes after their slot '
                      'without checking in. No-show rate = no-shows / scheduled bookings due in the period.'),
    'accuracy': ('Wait-time prediction accuracy',
                 'Each dot is a walk-in: the Random Forest estimate saved when it joined the queue (across) against how long it '
                 'actually took to be finished (up); dots on the dashed line were predicted exactly. Scheduled bookings are left '
                 "out - they aren't in line from the moment they book. MAE = the average difference; RMSE = the square root of "
                 'the average squared difference, which weighs big misses more (RMSE well above MAE means a few large misses). '
                 "The model retrains on the shop's own records every 6 hours."),
    'services': ('Service mix',
                 'Finished services in the period. Work time is start to finish of each job, so it shows which services take up '
                 "the staff's day."),
    'models': ('Car models',
               'Visits in the period by the car model last recorded for that plate. The ten most frequent are listed; the rest are '
               'counted together.'),
    'returns': ('How soon customers come back',
                'Each visit in the period by a vehicle (plate number) seen before: how long since its previous visit. The line '
                'shows the share of those return visits that came back within that many days.'),
    'staff': ('Staff performance',
              'Jobs: finished services they started. Pace: standard time / actual time (100% = on standard, higher = faster). '
              'The pace standard includes vehicle-size extra minutes. On time: clock-ins at or before 8:15 AM. '
              'Rating: average customer rating, shown from 5 reviews. MCDA uses a separate speed criterion '
              '(base service time / actual time, capped at 100%), punctuality and rating, weighted 40%, 40% and 20%. '
              'Speed and punctuality are divided by the team maximum; ratings are divided by 5.'),
}
_SELECT = """r.reservation_id, r.user_id, r.vehicle_plate_number, r.vehicle_type, r.vehicle_model, r.queue_status,
             r.booking_source, r.assigned_bay, r.booking_date, r.booking_time, r.created_at, r.arrived_at,
             r.started_at, r.completed_at, r.cancelled_at, r.cancel_reason, r.assigned_staff_id,
             r.predicted_wait_minutes, r.weather_condition, r.service_id,r.picked_up_at"""


# ---------------------------------------------------------------- time helpers
def _ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:19], STAMP)
    except (ValueError, AttributeError):
        return None


def _minutes(start, end):
    if not start or not end:
        return None
    diff = (end - start).total_seconds() / 60
    return diff if 0 <= diff <= MAX_PLAUSIBLE_SERVICE_MINUTES else None


def booked_at(row):
    if not row['booking_date'] or not row['booking_time']:
        return None
    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(f"{row['booking_date']} {row['booking_time']}", fmt)
        except (ValueError, TypeError):
            continue
    return None


def is_walk_in(row):
    return (row['booking_source'] or WALK_IN) == WALK_IN or booked_at(row) is None


def arrival_time(row):
    if is_walk_in(row):
        return _ts(row['created_at'])
    return _ts(row['arrived_at']) or booked_at(row)


def queue_wait_minutes(row):
    """Arrival (for a booking, no earlier than its booked time) to start of service."""
    started = _ts(row['started_at'])
    ready = arrival_time(row)
    if not started or not ready:
        return None
    if not is_walk_in(row):
        ready = max(ready, booked_at(row))
    return _minutes(min(ready, started), started)


def service_minutes(row):
    return _minutes(_ts(row['started_at']), _ts(row['completed_at']))


def hour_label(hour):
    return f"{(hour - 1) % 12 + 1} {'AM' if hour < 12 else 'PM'}"


def _day_label(day):
    return f"{day:%b} {day.day}"


def period_label(start, end):
    if start.year == end.year:
        return f"{_day_label(start)} – {_day_label(end)}, {end.year}"
    return f"{_day_label(start)}, {start.year} – {_day_label(end)}, {end.year}"


def parse_period(args, today=None):
    """(start, end) dates from ?days=7|30|90 or ?start=YYYY-MM-DD&end=YYYY-MM-DD.
    Raises ValueError with a message the page can show."""
    today = today or date.today()
    if args.get('start') or args.get('end'):
        try:
            start = datetime.strptime(args.get('start', ''), '%Y-%m-%d').date()
            end = datetime.strptime(args.get('end', ''), '%Y-%m-%d').date()
        except ValueError:
            raise ValueError('Pick both a start and an end date.')
        if end < start:
            raise ValueError('The end date is before the start date.')
        if end > today:
            raise ValueError("The end date can't be in the future.")
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            raise ValueError(f'Pick a period of at most {MAX_PERIOD_DAYS} days.')
        return start, end
    try:
        days = int(args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    if days not in PERIOD_PRESETS:
        days = 30
    return today - timedelta(days=days - 1), today


# ---------------------------------------------------------------- loading
def _load(cursor, source='all'):
    """Every booking, including the cancelled ones cleanup.py has moved to the archive."""
    rows = cursor.execute(f"""
        SELECT {_SELECT}, s.service_name, s.service_category, s.base_duration_minutes AS base_minutes
        FROM reservations r LEFT JOIN services s ON s.service_id = r.service_id
        WHERE {source_sql(source, 'r')}
        UNION ALL
        SELECT {_SELECT}, s.service_name, s.service_category, s.base_duration_minutes AS base_minutes
        FROM reservations_archive r LEFT JOIN services s ON s.service_id = r.service_id
        WHERE {source_sql(source, 'r')}
    """).fetchall()
    return [dict(row) for row in rows]


def _in(dt, start_dt, end_dt):
    return dt is not None and start_dt <= dt < end_dt


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _round(value, places=0):
    if value is None:
        return None
    return round(value, places) if places else int(round(value))


def _change(current, previous):
    if current is None or not previous:
        return None
    return _round((current - previous) / previous * 100)


# ---------------------------------------------------------------- MCDA criteria
def mcda_criteria(cursor, staff_id, since=None, until=None, source='real'):
    """The three SAW criteria for one staff member - Speed, Punctuality %,
    Rating - shared by the washer recommendation and automatic assignment
    (all time) and the Analytics staff table (one period), so all of them
    use one definition.

    - speed: average of min(100, service's set minutes / actual minutes * 100)
    - punctuality: days clocked in on time (Present) / days worked (Present
      or Late) - the newest attendance record of each day counts
    - rating: average customer rating on their jobs
    Each is None when there's nothing to compute it from."""
    window, params = '', [staff_id]
    if since and until:
        window = ' AND COALESCE(r.completed_at, r.cancelled_at) >= ? AND COALESCE(r.completed_at, r.cancelled_at) < ?'
        params += [since, until]
    service_query = " UNION ALL ".join(f"""
        SELECT r.queue_status, r.started_at, r.completed_at, s.base_duration_minutes
        FROM {table} r JOIN services s ON r.service_id = s.service_id
        WHERE r.assigned_staff_id = ? AND r.queue_status IN ('ready', 'completed', 'cancelled'){window}
          AND {source_sql(source, 'r')}
    """ for table in ('reservations', 'reservations_archive'))
    rows = cursor.execute(service_query, params * 2).fetchall()

    speed_scores = []
    for row in rows:
        if row['queue_status'] == 'cancelled':
            continue
        actual = _minutes(_ts(row['started_at']), _ts(row['completed_at']))
        if actual:
            speed_scores.append(min(100, row['base_duration_minutes'] / actual * 100))
    speed = round(sum(speed_scores) / len(speed_scores), 1) if speed_scores else None

    days_window, day_params = '', [staff_id]
    if since and until:
        days_window = ' AND a.date_str >= ? AND a.date_str < ?'
        day_params += [since[:10], until[:10]]
    days = cursor.execute(f'''
        SELECT a.status FROM staff_attendance a
        WHERE a.staff_user_id = ? AND a.status IN ('Present', 'Late'){days_window}
          AND {source_sql(source, 'a')}
          AND a.attendance_id = (SELECT MAX(b.attendance_id) FROM staff_attendance b
                                 WHERE b.staff_user_id = a.staff_user_id AND b.date_str = a.date_str
                                   AND {source_sql(source, 'b')})
    ''', day_params).fetchall()
    punctuality = round(sum(1 for d in days if d['status'] == 'Present') / len(days) * 100, 1) if days else None

    rating_row = cursor.execute(f"""
        SELECT AVG(f.rating) AS avg_rating, COUNT(*) AS n
        FROM feedbacks f JOIN reservations r ON f.reservation_id = r.reservation_id
        WHERE r.assigned_staff_id = ?{window} AND {source_sql(source, 'r')}
    """, params).fetchone()
    rating = round(rating_row['avg_rating'], 1) if rating_row['n'] else None
    return speed, punctuality, rating, rating_row['n']


# ---------------------------------------------------------------- the report
def build(start, end, now=None, source='all'):
    """Everything the Analytics page and its PDFs show, for start..end (dates, inclusive)."""
    now = now or datetime.now()
    source = parse_source(source)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    days = (end - start).days + 1
    prev_start_dt = start_dt - timedelta(days=days)
    today = now.date()
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = _load(cursor, source)
    extra = {r['type_name']: r['multiplier_minutes'] for r in cursor.execute('SELECT * FROM vehicle_types')}
    bays = len({r[0] for r in cursor.execute('SELECT DISTINCT default_bay FROM services WHERE is_active = 1')}) or 1
    staff_accounts = cursor.execute(
        "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY name").fetchall()
    attendance = _attendance(cursor, prev_start_dt.date(), end, source)
    feedback = cursor.execute(f"""
        SELECT f.rating, f.created_at, r.assigned_staff_id, r.completed_at
        FROM feedbacks f LEFT JOIN reservations r ON r.reservation_id = f.reservation_id
        WHERE {source_sql(source, 'r')}
    """).fetchall()

    for row in rows:
        row['_done'] = _ts(row['completed_at']) if row['queue_status'] in DONE else None
        row['_cancelled'] = _ts(row['cancelled_at']) if row['queue_status'] == 'cancelled' else None
        row['_arrival'] = arrival_time(row)
        row['_standard'] = (row['base_minutes'] or 0) + extra.get(row['vehicle_type'], 0)

    served = [r for r in rows if _in(r['_done'], start_dt, end_dt)]
    served_before = [r for r in rows if _in(r['_done'], prev_start_dt, start_dt)]
    # The shop's records start with its first finished service.
    first_record = min((r['_done'] for r in rows if r['_done']), default=None)
    comparable = first_record is not None and first_record <= prev_start_dt
    # Averages only count days the system was in use, and only days that are
    # over - a day still in progress, or before the first record, would drag them down.
    tracked_from = max(start, first_record.date()) if first_record else end + timedelta(days=1)
    tracked_days = max(0, (end - tracked_from).days + 1)
    full_days = [tracked_from + timedelta(days=i) for i in range(tracked_days) if tracked_from + timedelta(days=i) < today]

    visits = _visits(rows)
    kpis = _kpis(rows, served, served_before, start_dt, end_dt, prev_start_dt, now, attendance, feedback, visits, comparable)
    demand = _demand(rows, served, start, days, full_days, today, attendance, bays, tracked_from)
    operations = _operations(rows, served, start_dt, end_dt, now)
    pickup=[(_ts(r['picked_up_at'])-r['_done']).total_seconds()/60 for r in served
            if _ts(r['picked_up_at']) and _ts(r['picked_up_at'])>=r['_done']]
    pickup=[v for v in pickup if v is not None]
    operations['lifecycle']={'queue_wait':operations['avg_wait'],
        'service':_round(_avg(service_minutes(r) for r in served)),
        'pickup_delay':_round(_avg(pickup)),'pickup_samples':len(pickup),
        'definition':'Queue wait: arrival to service start. Service: start to finish. Pickup delay: finish to collection.'}
    evaluation=cursor.execute('SELECT * FROM model_evaluations ORDER BY evaluation_id DESC LIMIT 1').fetchone()
    operations['model_validation']=dict(evaluation) if evaluation and source=='real' else None
    operations['accuracy']['definition']='Saved walk-in arrival-to-finish estimates compared with later recorded completion times; distinct from queue wait.'
    operations['accuracy']['evaluation_from']=start.isoformat()
    operations['accuracy']['evaluation_until']=end.isoformat()
    operations['accuracy']['evidence_note'] = (
        'Prediction errors on generated records demonstrate simulated behavior; they are not field validation.'
        if source == 'synthetic' else
        'Includes generated records; these prediction errors are not field validation.'
        if source == 'all' else
        'Errors use saved predictions on records marked real. A chronological holdout is reported separately.')
    operations['accuracy']['plotted_samples'] = len(operations['accuracy']['points'])
    customers = _customers(served, visits, start, end)
    staff = _staff(cursor, staff_accounts, served, attendance, feedback, start, end, start_dt, end_dt, source)
    conn.close()

    report = {
        'source': {'key': source, 'label': 'System records' if source == 'all' else SOURCES[source],
                   'note': 'Includes synthetic history; these figures do not establish real-world prediction accuracy.' if source != 'real'
                           else 'Real records only. Synthetic history is excluded from operational recommendations.'},
        'period': {
            'start': start.isoformat(), 'end': end.isoformat(), 'days': days, 'label': period_label(start, end),
            'comparable': comparable, 'first_record': first_record.date().isoformat() if first_record else None,
            'tracked_from': tracked_from.isoformat() if tracked_days else None, 'tracked_days': tracked_days,
        },
        'generated': now.strftime('%B %d, %Y %I:%M %p').replace(' 0', ' '),
        'kpis': kpis,
        'demand': demand,
        'operations': operations,
        'customers': customers,
        'staff': staff,
    }
    staffing = demand['staffing']
    report['sections'] = SECTIONS
    report['blocks'] = {key: {'title': title, 'method': method.format(**staffing) if key == 'staffing' else method}
                        for key, (title, method) in BLOCKS.items()}
    _write_takeaways(report)
    return report


def _attendance(cursor, first_day, last_day, source='all'):
    """{date: {staff_user_id: latest record that day}} - insert-only log, the newest row wins."""
    rows = cursor.execute(f"""
        SELECT a.staff_user_id, a.date_str, a.status, a.time_in, a.time_out
        FROM staff_attendance a
        WHERE a.staff_user_id IS NOT NULL AND a.date_str >= ? AND a.date_str <= ?
          AND {source_sql(source, 'a')}
          AND a.attendance_id = (SELECT MAX(b.attendance_id) FROM staff_attendance b
                                 WHERE b.staff_user_id = a.staff_user_id AND b.date_str = a.date_str
                                   AND {source_sql(source, 'b')})
    """, (first_day.isoformat(), last_day.isoformat())).fetchall()
    by_day = defaultdict(dict)
    for row in rows:
        try:
            by_day[datetime.strptime(row['date_str'], '%Y-%m-%d').date()][row['staff_user_id']] = row
        except ValueError:
            continue
    return by_day


def _worked(record):
    return record['status'] in ('Present', 'Late')


def _visits(rows):
    """{plate key: sorted list of visit days} - vehicles.py's definition, archive included."""
    by_plate = defaultdict(set)
    models = {}
    for row in sorted((r for r in rows if r['_done']), key=lambda r: r['_done']):
        key = plate_key(row['vehicle_plate_number'])
        if key:
            by_plate[key].add(row['_done'].date())
            if row['vehicle_model']:
                models[key] = row['vehicle_model']
    return {key: {'days': sorted(days), 'model': models.get(key)} for key, days in by_plate.items()}


def _visits_in(visits, start, end):
    """(plate, day, previous visit day or None) for each visit in the period."""
    found = []
    for key, vehicle in visits.items():
        previous = None
        for day in vehicle['days']:
            if start <= day <= end:
                found.append((key, day, previous))
            previous = day
    return found


# ---------------------------------------------------------------- KPIs
def _kpis(rows, served, served_before, start_dt, end_dt, prev_start_dt, now, attendance, feedback, visits, comparable):
    def figures(sel, a, b):
        due = [r for r in rows if not is_walk_in(r) and _in(booked_at(r), a, min(b, now))
               and not (r['queue_status'] == 'cancelled' and r['cancel_reason'] in ('customer', 'staff')
                        and r['_cancelled'] and r['_cancelled'] < booked_at(r))]
        no_shows = sum(1 for r in due if r['queue_status'] == 'cancelled' and r['cancel_reason'] == 'no_show')
        ratings = [f['rating'] for f in feedback if _in(_ts(f['created_at']), a, b)]
        period_visits = _visits_in(visits, a.date(), (b - timedelta(days=1)).date())
        shifts = sum(1 for day, staff in attendance.items() if a.date() <= day < b.date() for rec in staff.values() if _worked(rec))
        return {
            'served': len(sel),
            'wait': _avg(queue_wait_minutes(r) for r in sel),
            'no_show': no_shows / len(due) * 100 if due else None,
            'rating': _avg(ratings), 'ratings': len(ratings),
            'returning': sum(1 for v in period_visits if v[2]) / len(period_visits) * 100 if period_visits else None,
            'per_shift': len(sel) / shifts if shifts else None,
        }

    cur = figures(served, start_dt, end_dt)
    old = figures(served_before, prev_start_dt, start_dt) if comparable else {}

    def kpi(key, label, value, display, better, note=None):
        return {'key': key, 'label': label, 'value': value, 'display': display, 'better': better, 'note': note,
                'change': _change(value, old.get(key)) if comparable else None}

    rating_note = f"{cur['ratings']} review{'' if cur['ratings'] == 1 else 's'}"
    return [
        kpi('served', 'Cars served', cur['served'], f"{cur['served']:,}", 'up'),
        kpi('wait', 'Avg queue wait', cur['wait'], f"{_round(cur['wait'])} min" if cur['wait'] is not None else '–', 'down'),
        kpi('no_show', 'No-show rate', cur['no_show'], f"{cur['no_show']:.1f}%" if cur['no_show'] is not None else '–', 'down',
            None if cur['no_show'] is not None else 'No scheduled bookings'),
        kpi('rating', 'Avg rating', cur['rating'], f"{cur['rating']:.1f} / 5" if cur['rating'] is not None else '–', 'up', rating_note),
        kpi('returning', 'Returning customers', cur['returning'], f"{_round(cur['returning'])}%" if cur['returning'] is not None else '–', 'up'),
        kpi('per_shift', 'Cars per staff shift', cur['per_shift'], f"{cur['per_shift']:.1f}" if cur['per_shift'] is not None else '–', 'up'),
    ]


# ---------------------------------------------------------------- Demand
def _demand(rows, served, start, days, full_days, today, attendance, bays, tracked_from):
    per_day = Counter(r['_done'].date() for r in served)
    votes = defaultdict(Counter)
    for row in rows:  # weather is recorded on each booking as it's made
        created = _ts(row['created_at'])
        if row['weather_condition'] and created:
            votes[created.date()][row['weather_condition']] += 1
    weather_of = {day: v.most_common(1)[0][0] for day, v in votes.items()}

    daily = []
    for i in range(days):
        day = start + timedelta(days=i)
        daily.append({'date': day.isoformat(), 'label': _day_label(day), 'cars': per_day.get(day, 0),
                      'weather': weather_of.get(day), 'partial': day >= today, 'tracked': day >= tracked_from})

    # Trailing 7-day average (days with records only) - the trend under the day-to-day noise.
    tracked = [x for x in daily if x['tracked'] and not x['partial']]
    for i, x in enumerate(tracked):
        window = tracked[max(0, i - 6):i + 1]
        x['avg7'] = round(sum(w['cars'] for w in window) / len(window), 1) if len(window) >= 3 else None

    occurrences = Counter(day.weekday() for day in full_days)
    full = set(full_days)
    arrivals = Counter()
    for r in served:
        a = r['_arrival']
        if a and a.date() in full and a.hour in HOURS:
            arrivals[(a.weekday(), a.hour)] += 1
    heatmap = [[round(arrivals[(wd, h)] / occurrences[wd], 1) if occurrences[wd] else 0 for h in HOURS] for wd in range(7)]
    weekday_avg = [round(sum(per_day.get(d, 0) for d in full_days if d.weekday() == wd) / occurrences[wd], 1)
                   if occurrences[wd] else None for wd in range(7)]

    by_weather = defaultdict(list)
    for day in full_days:
        if day in weather_of:
            by_weather[weather_of[day]].append(per_day.get(day, 0))
    weather = [{'condition': w, 'avg': round(_avg(by_weather[w]), 1), 'days': len(by_weather[w])}
               for w in ('Sunny', 'Cloudy', 'Rainy') if by_weather.get(w)]

    avg_service = _avg(service_minutes(r) for r in served)
    crew = get_setting('crew_per_car')
    peak = [max(row) for row in heatmap]
    needed, on_duty = [], []
    for wd in range(7):
        if not occurrences[wd] or avg_service is None:
            needed.append(None)
        else:
            at_once = min(bays, peak[wd] * avg_service / 60)
            needed.append(max(MIN_STAFF_ON_DUTY, math.ceil(round(at_once * crew, 2))))
        counts = [sum(1 for rec in attendance.get(d, {}).values() if _worked(rec)) for d in full_days if d.weekday() == wd]
        on_duty.append(round(sum(counts) / len(counts), 1) if counts else None)

    busiest = max(((wd, i) for wd in range(7) for i in range(len(HOURS))), key=lambda x: heatmap[x[0]][x[1]], default=None)
    return {
        'daily': daily,
        'hours': [hour_label(h) for h in HOURS],
        'heatmap': heatmap,
        'busiest': {'weekday': WEEKDAYS[busiest[0]], 'hour': hour_label(HOURS[busiest[1]]),
                    'until': hour_label(HOURS[busiest[1]] + 1), 'cars': heatmap[busiest[0]][busiest[1]]}
                   if busiest and heatmap[busiest[0]][busiest[1]] else None,
        'weekday_avg': weekday_avg,
        'weather': weather,
        'staffing': {'needed': needed, 'on_duty': on_duty, 'peak_cars': peak, 'avg_service': _round(avg_service),
                     'days_sampled': [occurrences[wd] for wd in range(7)],
                     'crew': crew, 'bays': bays, 'minimum': MIN_STAFF_ON_DUTY},
    }


# ---------------------------------------------------------------- Operations
def _operations(rows, served, start_dt, end_dt, now):
    wait_by_hour = []
    for h in HOURS:
        waits = [queue_wait_minutes(r) for r in served if r['_arrival'] and r['_arrival'].hour == h]
        waits = [w for w in waits if w is not None]
        wait_by_hour.append({'hour': hour_label(h), 'minutes': _round(_avg(waits)), 'cars': len(waits)})

    by_service = defaultdict(list)
    for r in served:
        actual = service_minutes(r)
        if actual is not None and r['service_name']:
            by_service[r['service_name']].append((actual, r['_standard']))
    service_times = sorted(({'service': name, 'jobs': len(v), 'standard': _round(_avg(s for _, s in v)),
                             'actual': _round(_avg(a for a, _ in v))} for name, v in by_service.items()),
                           key=lambda x: (-x['jobs'], x['service']))[:8]

    cancelled = Counter(CANCEL_REASONS.get(r['cancel_reason'], 'Other') for r in rows if _in(r['_cancelled'], start_dt, end_dt))

    errors, points = [], []
    for r in sorted(served, key=lambda r: r['_done']):
        if is_walk_in(r) and r['predicted_wait_minutes'] is not None:
            actual = _minutes(_ts(r['created_at']), r['_done'])
            if actual is not None:
                errors.append(abs(actual - r['predicted_wait_minutes']))
                points.append([round(r['predicted_wait_minutes']), round(actual)])
    return {
        'wait_by_hour': wait_by_hour,
        'avg_wait': _round(_avg(queue_wait_minutes(r) for r in served)),
        'service_times': service_times,
        'cancellations': [{'reason': k, 'count': v} for k, v in cancelled.most_common()],
        # MAE: the average miss. RMSE: squares each miss first, so a few
        # big misses raise it more than many small ones - RMSE well above
        # MAE means the errors are uneven. Both in minutes, to 0.1.
        'accuracy': {'mae': _round(_avg(errors), 1),
                     'rmse': _round(_avg(e * e for e in errors) ** 0.5, 1) if errors else None,
                     'within_15': _round(sum(1 for e in errors if e <= 15) / len(errors) * 100) if errors else None,
                     'samples': len(errors), 'points': points[-MAX_ACCURACY_POINTS:]},
    }


# ---------------------------------------------------------------- Services & customers
def _customers(served, visits, start, end):
    counts = Counter(r['service_name'] or 'Unknown service' for r in served)
    work = defaultdict(float)
    for r in served:
        minutes = service_minutes(r)
        if minutes:
            work[r['service_name'] or 'Unknown service'] += minutes
    total_cars, total_work = sum(counts.values()), sum(work.values())
    top = counts.most_common(7)
    services = [{'service': s, 'cars': n, 'share': n / total_cars * 100, 'work_share': work[s] / total_work * 100 if total_work else None}
                for s, n in top]
    rest = total_cars - sum(n for _, n in top)
    if rest:
        services.append({'service': f'{len(counts) - len(top)} other services', 'cars': rest, 'share': rest / total_cars * 100,
                         'work_share': 100 - sum(work[s] for s, _ in top) / total_work * 100 if total_work else None, 'group': True,
                         'services': len(counts) - len(top)})
    detailing = {r['service_name'] for r in served if r['service_category'] == 'detailing'}

    period_visits = _visits_in(visits, start, end)
    models = Counter(visits[key]['model'] or 'Not recorded' for key, _, _ in period_visits)
    unrecorded = models.pop('Not recorded', 0)
    top_models = models.most_common(10)

    gaps = [(day - previous).days for _, day, previous in period_visits if previous]
    buckets = [('Within a week', 0, 7), ('1–2 weeks', 8, 14), ('2–4 weeks', 15, 30), ('Over a month', 31, None)]
    return {
        'services': services,
        'detailing': {'cars': sum(counts[s] for s in detailing) / total_cars * 100 if total_cars else None,
                      'work': sum(work[s] for s in detailing) / total_work * 100 if total_work else None},
        'models': [{'model': m, 'visits': n} for m, n in top_models],
        'other_models': len(models) - len(top_models),
        'other_model_visits': sum(models.values()) - sum(n for _, n in top_models),
        'unrecorded_visits': unrecorded,
        'returns': [{'label': label, 'visits': sum(1 for g in gaps if g >= lo and (hi is None or g <= hi))} for label, lo, hi in buckets],
        'return_visits': len(gaps),
        'visits': len(period_visits),
        'typical_return_days': _round(median(gaps)) if gaps else None,
        # Share of return visits that came back within N days, N = 0..RETURN_CURVE_DAYS.
        'return_curve': [{'days': n, 'share': round(sum(1 for g in gaps if g <= n) / len(gaps) * 100, 1)}
                         for n in range(RETURN_CURVE_DAYS + 1)] if gaps else [],
    }


# ---------------------------------------------------------------- Staff
def _staff(cursor, accounts, served, attendance, feedback, start, end, start_dt, end_dt, source='all'):
    since, until = start_dt.strftime(STAMP), end_dt.strftime(STAMP)
    rows, criteria = [], []
    for account in accounts:
        sid = account['user_id']
        jobs = [r for r in served if r['assigned_staff_id'] == sid]
        records = [staff[sid] for day, staff in attendance.items() if start <= day <= end and sid in staff]
        worked = [rec for rec in records if _worked(rec)]
        paces = [r['_standard'] / service_minutes(r) * 100 for r in jobs if service_minutes(r) and r['_standard']]
        speed, punctuality, rating, reviews = mcda_criteria(cursor, sid, since, until, source=source)
        rows.append({
            'name': account['name'], 'jobs': len(jobs),
            'per_shift': round(len(jobs) / len(worked), 1) if worked else None,
            'pace': _round(_avg(paces)),
            'on_time': _round(sum(1 for rec in worked if rec['status'] == 'Present') / len(worked) * 100) if worked else None,
            'absences': sum(1 for rec in records if rec['status'] == 'Absent'),
            'rating': rating, 'reviews': reviews,
            'speed': speed, 'punctuality': punctuality,
        })
        criteria.append({'name': account['name'], 'service_speed': speed, 'punctuality': punctuality, 'feedback': rating})

    scores = {r['name']: r for r in mcda_engine.evaluate_staff(criteria)}
    for row in rows:
        ranked = scores.get(row['name'])
        has_record = row['speed'] is not None or row['punctuality'] is not None or row['rating'] is not None
        row['score'] = ranked['saw_score'] if ranked and has_record else None
    rows.sort(key=lambda r: (r['score'] is None, -(r['score'] or 0), -r['jobs'], r['name']))
    for rank, row in enumerate(rows, start=1):
        row['rank'] = rank if row['score'] is not None else None
    return {'rows': rows, 'weights': {'speed': 40, 'punctuality': 40, 'rating': 20}}


# ---------------------------------------------------------------- takeaways
def _write_takeaways(report):
    """One or two plain sentences per chart, built only from the numbers above."""
    k = {item['key']: item for item in report['kpis']}
    d, o, c, s = report['demand'], report['operations'], report['customers'], report['staff']
    days = report['period']['days']
    tracked = report['period']['tracked_days']
    served = k['served']['value']
    t = {}

    if not served:
        t['daily'] = 'No cars were served in this period.'
    else:
        t['daily'] = f"{served:,} cars in {days} day{'s' if days != 1 else ''}, about {served / max(tracked, 1):.0f} a day."
        if tracked < days:
            t['daily'] = (f"{served:,} cars in the {tracked} day{'s' if tracked != 1 else ''} with records "
                          f"(records start {_day_label(date.fromisoformat(report['period']['tracked_from']))}), about {served / max(tracked, 1):.0f} a day.")
        if k['served']['change'] is not None:
            t['daily'] += f" {abs(k['served']['change'])}% {'more' if k['served']['change'] >= 0 else 'fewer'} than the {days} days before."

    weekday_avg = d['weekday_avg']
    known = [(wd, v) for wd, v in enumerate(weekday_avg) if v is not None]
    if d['busiest'] and known:
        top_wd, low_wd = max(known, key=lambda x: x[1]), min(known, key=lambda x: x[1])
        t['heatmap'] = (f"Busiest: {d['busiest']['weekday']} {d['busiest']['hour']}–{d['busiest']['until']} "
                        f"(avg {d['busiest']['cars']} cars). {WEEKDAYS[top_wd[0]]}s average {top_wd[1]} cars a day, "
                        f"{WEEKDAYS[low_wd[0]]}s {low_wd[1]}.")
    else:
        t['heatmap'] = 'Not enough finished days in this period to show a pattern yet.'

    weather = {w['condition']: w for w in d['weather']}
    dry = [w for name, w in weather.items() if name != 'Rainy']
    if not weather:
        t['weather'] = 'No weather was recorded on bookings in this period.'
    elif 'Rainy' in weather and dry:
        dry_avg = sum(w['avg'] * w['days'] for w in dry) / sum(w['days'] for w in dry)
        drop = _round((1 - weather['Rainy']['avg'] / dry_avg) * 100) if dry_avg else 0
        t['weather'] = (f"Rainy days averaged {weather['Rainy']['avg']} cars, {abs(drop)}% {'fewer' if drop >= 0 else 'more'} "
                        f"than dry days ({dry_avg:.1f}).")
    else:
        t['weather'] = 'Need both rainy and dry days in the period to compare.'

    st = d['staffing']
    pairs = [(wd, st['needed'][wd], st['on_duty'][wd]) for wd in range(7) if st['needed'][wd] is not None and st['on_duty'][wd] is not None]
    short = [p for p in pairs if p[1] - p[2] >= 0.5]
    spare = [p for p in pairs if p[2] - p[1] >= 1]
    parts = []
    if short:
        wd, need, have = short[0]
        parts.append(f"Short on {', '.join(WEEKDAYS[p[0]][:3] for p in short)} (e.g. {WEEKDAYS[wd][:3]}: {need} needed, {have} on duty).")
    if spare:
        parts.append(f"One fewer would still cover {', '.join(WEEKDAYS[p[0]][:3] for p in spare)}.")
    t['staffing'] = ' '.join(parts) if parts else ('Staffing matched demand on every weekday.' if pairs else 'Not enough attendance and service records to compare yet.')

    waits = [w for w in o['wait_by_hour'] if w['cars'] >= MIN_SAMPLE and w['minutes'] is not None]
    if o['avg_wait'] is None:
        t['wait'] = 'No started services in this period.'
    else:
        t['wait'] = f"Average queue wait {o['avg_wait']} min."
        if waits:
            worst = max(waits, key=lambda w: w['minutes'])
            t['wait'] += f" Longest for cars arriving around {worst['hour']} ({worst['minutes']} min)."

    slower = [x for x in o['service_times'] if x['jobs'] >= MIN_SAMPLE and x['actual'] - x['standard'] >= 5]
    t['service_times'] = (f"{slower[0]['service']} ran {slower[0]['actual'] - slower[0]['standard']} min over its standard on average."
                          if slower else 'Services finished within 5 minutes of their standard time.' if o['service_times'] else
                          'No finished services in this period.')

    total_cancelled = sum(x['count'] for x in o['cancellations'])
    t['cancellations'] = (f"{total_cancelled} booking{'s' if total_cancelled != 1 else ''} didn't go ahead."
                          + (f" No-show rate: {k['no_show']['display']} of scheduled bookings." if k['no_show']['value'] is not None else '')
                          if total_cancelled else 'Every booking in this period went ahead.')

    acc = o['accuracy']
    t['accuracy'] = (f"Wait estimates for walk-ins were off by {acc['mae']} min on average (MAE; RMSE {acc['rmse']} min); "
                     f"{acc['within_15']}% were within 15 minutes, across {acc['samples']} walk-in{'' if acc['samples'] == 1 else 's'}."
                     if acc['samples'] else 'No walk-ins with a saved wait estimate in this period.')

    if c['services']:
        first = c['services'][0]
        t['services'] = f"{first['service']} is {first['share']:.0f}% of cars."
        det = c['detailing']
        if det['cars'] and det['work'] and det['work'] - det['cars'] >= 3:
            t['services'] += f" Detailing is only {det['cars']:.0f}% of cars but takes {det['work']:.0f}% of staff working time."
    else:
        t['services'] = 'No finished services in this period.'

    if c['models']:
        t['models'] = f"Most common: {c['models'][0]['model']} ({c['models'][0]['visits']} visits)."
        if c['other_models']:
            t['models'] += f" {c['other_models']} other models account for {c['other_model_visits']} more."
    else:
        t['models'] = 'No car models recorded on visits in this period.'

    t['returns'] = (f"{c['return_visits']} of {c['visits']} visits were by returning vehicles; "
                    f"a typical one came back after {c['typical_return_days']} days."
                    if c['return_visits'] else 'No returning vehicles in this period yet.')

    ranked = [r for r in s['rows'] if r['score'] is not None]
    busiest = max(s['rows'], key=lambda r: r['jobs'], default=None)
    if ranked:
        t['staff'] = f"{ranked[0]['name']} ranks first on the MCDA score ({ranked[0]['score']:.3f})."
        if busiest and busiest['jobs'] and busiest['name'] != ranked[0]['name']:
            t['staff'] += f" {busiest['name']} handled the most jobs ({busiest['jobs']})."
    else:
        t['staff'] = 'No staff activity recorded in this period.'
    report['takeaways'] = t
