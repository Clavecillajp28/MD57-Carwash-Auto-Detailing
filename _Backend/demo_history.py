"""Opt-in, atomic demo history. Never represents observed shop operations."""
from datetime import date, datetime, time, timedelta
import json
import random
import secrets
from zoneinfo import ZoneInfo

from database import get_db_connection
from werkzeug.security import generate_password_hash

BATCH = 'demo-history-60-workdays-v1'
STAMP = '%Y-%m-%d %H:%M:%S'


def workdays(end):
    """The 60 Monday-Friday dates ending on or before end."""
    days = []
    while len(days) < 60:
        if end.weekday() < 5:
            days.append(end)
        end -= timedelta(days=1)
    return list(reversed(days))


def _complete_pickup_details(conn, summary):
    """Upgrade only this batch's fictional records, including archived history."""
    ids = summary.get('staff_ids', [])
    if not ids:
        return
    for table in ('reservations', 'reservations_archive'):
        conn.execute(f'''UPDATE {table} SET
            picked_up_at=COALESCE(NULLIF(picked_up_at,''),datetime(completed_at,'+5 minutes')),
            picked_up_by=COALESCE(picked_up_by,assigned_staff_id),
            picked_up_by_name=COALESCE(NULLIF(TRIM(picked_up_by_name),''),
                (SELECT COALESCE(fullname,username) FROM users WHERE user_id=COALESCE({table}.picked_up_by,{table}.assigned_staff_id)))
            WHERE is_synthetic=1 AND queue_status='completed' AND completed_at IS NOT NULL
                AND assigned_staff_id IN ({','.join('?' for _ in ids)})
                AND booking_date BETWEEN ? AND ? AND vehicle_plate_number LIKE 'DEMO-%'
                AND (picked_up_at IS NULL OR picked_up_at='' OR picked_up_by IS NULL
                     OR picked_up_by_name IS NULL OR TRIM(picked_up_by_name)='')''',
            [*ids, summary['start'], summary['end']])


def seed_demo_history(end=None):
    """Add one identifiable batch; repeated starts do not duplicate it.

    Existing live accounts and records are never updated. Demo staff cannot log in.
    No notifications are sent, and no model predictions or real accuracy
    measurements are invented. Date defaults use the shop timezone.
    """
    if end is None:
        end = datetime.now(ZoneInfo('Asia/Manila')).date() - timedelta(days=1)
    if not isinstance(end, date) or end >= datetime.now(ZoneInfo('Asia/Manila')).date():
        raise ValueError('Demo history must end before today in Asia/Manila.')
    days = workdays(end)
    rng = random.Random(57060)
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''CREATE TABLE IF NOT EXISTS demo_history_batches (
            batch_key TEXT PRIMARY KEY, summary_json TEXT NOT NULL)''')
        prior = conn.execute('SELECT summary_json FROM demo_history_batches WHERE batch_key=?', (BATCH,)).fetchone()
        if prior:
            summary = json.loads(prior[0])
            _complete_pickup_details(conn, summary)
            conn.commit()
            return {**summary, 'already_loaded': True}
        catalog = [dict(r) for r in conn.execute('''
            SELECT s.service_id,s.service_name,s.service_type,s.default_bay,
                s.base_duration_minutes,p.vehicle_type,p.price,v.multiplier_minutes
            FROM services s JOIN service_prices p ON p.service_id=s.service_id
            JOIN vehicle_types v ON v.type_name=p.vehicle_type
            WHERE s.is_active=1 AND s.base_duration_minutes BETWEEN 10 AND 90
            ORDER BY s.service_id,p.vehicle_type''')]
        bays = ('Bay #1', 'Bay #2', 'Bay #3')
        vehicles = {
            'Small': ('S', 'Toyota Wigo'), 'Medium': ('M', 'Toyota Vios'),
            'Large': ('L', 'Toyota Fortuner'), 'XLarge': ('XL', 'Toyota Hiace'),
            'Motor (S)': ('MS', 'Honda Click'), 'Motor (L)': ('ML', 'Yamaha NMAX'),
        }
        choices = {bay: [r for r in catalog if r['default_bay'] == bay] for bay in bays}
        if any(not values for values in choices.values()):
            raise ValueError('Demo history needs an active priced service for each of the three bays.')
        staff = []
        # One salted password hash can be shared: each account is disabled and
        # the random password is discarded rather than offered as a demo login.
        password = generate_password_hash(secrets.token_urlsafe(32))
        now = datetime.now(ZoneInfo('Asia/Manila')).strftime(STAMP)
        for number in range(1, 4):
            name = f'Demo Staff {number}'
            user_id = conn.execute('''INSERT INTO users
                (username,fullname,password_hash,role,disabled_at,created_at)
                VALUES (?,?,?,'staff',?,?)''',
                (f'demo60-staff-{number}-{secrets.token_hex(4)}', name, password, now, now)).lastrowid
            staff.append((user_id, name))
        totals = dict(workdays=60, start=days[0].isoformat(), end=days[-1].isoformat(),
                      reservations=0, completed=0, cancelled=0, attendance=0, feedback=0,
                      source='synthetic', staff_ids=[s[0] for s in staff])
        for day in days:
            opening = datetime.combine(day, time(8))
            closing = datetime.combine(day, time(21))
            available = {bay: opening for bay in bays}
            active = {bay: [] for bay in bays}
            weather = rng.choices(['Sunny', 'Cloudy', 'Rainy'], [6, 3, 2])[0]
            rain = round(rng.uniform(0.5, 7), 1) if weather == 'Rainy' else 0
            for user_id, name in staff:
                clock_in = opening + timedelta(minutes=rng.randint(-8, 25))
                conn.execute('''INSERT INTO staff_attendance
                    (staff_user_id,staff_name,date_str,status,time_in,time_out,created_at,is_synthetic)
                    VALUES (?,?,?,?,?,?,?,1)''', (user_id, name, day.isoformat(),
                    'Late' if clock_in > opening + timedelta(minutes=15) else 'Present',
                    clock_in.strftime('%I:%M %p'), '09:00 PM', opening.strftime(STAMP)))
                totals['attendance'] += 1
            arrivals = sorted(rng.sample(range(40, 600), rng.randint(20, 30)))
            for number, minute in enumerate(arrivals, 1):
                arrival = opening + timedelta(minutes=minute)
                bay = rng.choices(bays, [5, 4, 2])[0]
                service = rng.choice(choices[bay])
                user_id, staff_name = staff[bays.index(bay)]
                standard = service['base_duration_minutes'] + service['multiplier_minutes']
                duration = max(10, round(standard * rng.uniform(0.8, 1.3) + rain))
                start = max(arrival, available[bay])
                finish = start + timedelta(minutes=duration)
                if finish > closing - timedelta(minutes=20):
                    continue
                cancelled = rng.random() < 0.07
                ahead = max(0, (available[bay] - arrival).total_seconds() / 60)
                queue_length = sum(done > arrival for done in active[bay])
                size_code, model = vehicles.get(service['vehicle_type'], ('X', 'Demo Vehicle'))
                plate = f'DEMO-{size_code}-{rng.randint(1, 60):04d}'
                customer = f'Demo Customer {size_code}{int(plate[-4:]):03d}'
                scheduled = rng.random() < 0.3
                row = {
                    'customer_name': customer, 'vehicle_plate_number': plate,
                    'vehicle_type': service['vehicle_type'], 'service_id': service['service_id'],
                    'vehicle_model': model,
                    'queue_number': number, 'queue_status': 'cancelled' if cancelled else 'completed',
                    'booking_source': 'Online Booking' if scheduled else 'Walk-in Customer',
                    'assigned_bay': bay, 'booking_date': day.isoformat(),
                    'booking_time': arrival.strftime('%I:%M %p'),
                    'created_at': (arrival - timedelta(hours=2) if scheduled else arrival).strftime(STAMP),
                    'arrived_at': None if cancelled else arrival.strftime(STAMP),
                    'started_at': None if cancelled else start.strftime(STAMP),
                    'completed_at': None if cancelled else finish.strftime(STAMP),
                    'picked_up_at': None if cancelled else (finish + timedelta(minutes=rng.randint(3, 15))).strftime(STAMP),
                    'picked_up_by': None if cancelled else user_id,
                    'picked_up_by_name': None if cancelled else staff_name,
                    'cancelled_at': (arrival + timedelta(minutes=15)).strftime(STAMP) if cancelled else None,
                    'cancel_reason': 'customer' if cancelled else None,
                    'assigned_staff_id': user_id, 'price': service['price'],
                    'weather_condition': weather, 'precipitation_mm': rain,
                    'queue_length_snapshot': queue_length, 'active_staff_snapshot': 3,
                    'work_ahead_minutes': ahead, 'is_synthetic': 1,
                }
                columns = ','.join(row)
                reservation_id = conn.execute(f'INSERT INTO reservations ({columns}) VALUES ({",".join("?" for _ in row)})',
                                               list(row.values())).lastrowid
                totals['reservations'] += 1
                totals['cancelled' if cancelled else 'completed'] += 1
                if not cancelled:
                    available[bay] = finish
                    active[bay].append(finish)
                    if rng.random() < 0.55:
                        conn.execute('''INSERT INTO feedbacks
                            (reservation_id,customer_name,plate_number,service_type,rating,comment,created_at)
                            VALUES (?,?,?,?,?,?,?)''', (reservation_id, customer, plate,
                            service['service_type'], rng.choices([2, 3, 4, 5], [1, 2, 5, 5])[0],
                            'Simulated feedback for the 60-workday demonstration.',
                            (finish + timedelta(minutes=20)).strftime(STAMP)))
                        totals['feedback'] += 1
        conn.execute('INSERT INTO demo_history_batches VALUES (?,?)', (BATCH, json.dumps(totals)))
        conn.execute('''INSERT INTO audit_records (occurred_at,actor_name,entity,entity_id,action,reason)
            VALUES (?,'System','demo_history',?,'insert',?)''',
            (now, BATCH, 'Loaded 60 simulated workdays for demonstration, not real accuracy evidence.'))
        conn.commit()
        return {**totals, 'already_loaded': False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
