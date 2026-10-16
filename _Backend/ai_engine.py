"""Random Forest turnaround prediction and MCDA staff scoring."""

from shop_clock import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

class WaitTimePredictorAI:
    # Below this many real completed bookings (with a full snapshot - see
    # ai_retrain.py), real samples are too few relative to noise to trust
    # blending in, so retraining is skipped and the current model is kept.
    MIN_REAL_SAMPLES_TO_RETRAIN = 20
    # From this many real samples on, the synthetic bootstrap is dropped and
    # the model learns from real bookings only.
    REAL_ONLY_MIN_SAMPLES = 200
    # Service durations the synthetic data spans - the real catalog's range
    # (15-180 min) - and the fallback when a prediction gets none.
    SYNTHETIC_SERVICE_MINUTES = [15, 20, 30, 35, 45, 60, 90, 180]
    DEFAULT_SERVICE_MINUTES = 30
    # The inputs, in order. work_ahead_minutes (the car in the bay plus the
    # cars queued before it, in standard minutes - wait_estimate.py) and the
    # hour of day were added 2026-10-06: queue_length only counts cars booked
    # for the same *service* anywhere in the shop, which says little about
    # how long this bay will actually take.
    FEATURES = ['queue_length', 'active_staff', 'precipitation', 'service_base_time', 'work_ahead_minutes', 'hour']

    def __init__(self):
        self.last_retrain_sample_count = 0
        self.trained_on = 'synthetic'  # then 'blended' or 'real' after retraining
        X, y = self._synthetic_training_data()
        self.model = self._fit_new_model(X, y)
        print("[AI ENGINE] Random Forest Model for Wait-Time prediction initialized and trained successfully.")

    def _synthetic_training_data(self):
        """
        Gumagawa ng initial historical logs para may paunang laman ang AI model bago ikabit sa live DB.
        Sinasanay nito ang AI na intindihin ang epekto ng pila, bilang ng staff, at ulan. Also used as a
        floor/regularizer blended into real data during retraining (see retrain_with_real_data), so a
        still-small real sample doesn't swing predictions wildly.
        """
        rng = np.random.default_rng(42)
        n_samples = 600

        # Mga input features na tinukoy sa inyong Feature Engineering
        queue_length = rng.integers(0, 8, n_samples)                       # Bilang ng sasakyan sa pila (same service)
        active_staff = rng.integers(2, 6, n_samples)                       # Bilang ng active washers sa shift
        precipitation = np.where(rng.random(n_samples) < 0.35, rng.uniform(0.2, 12.0, n_samples), 0.0)  # Ulan sa mm
        service_base_time = rng.choice(self.SYNTHETIC_SERVICE_MINUTES, n_samples)  # Tagal ng serbisyo (minuto)
        cars_ahead = rng.integers(0, 6, n_samples)                          # Sasakyang nauna sa parehong bay
        work_ahead = np.array([rng.choice(self.SYNTHETIC_SERVICE_MINUTES, k).sum() * rng.uniform(0.5, 1.0) if k else 0.0
                               for k in cars_ahead])                         # Minuto ng trabahong nauna sa bay
        hour = rng.integers(8, 21, n_samples)

        # Target: hintay sa bay (ang trabahong nauna) + sariling serbisyo,
        # mas mabagal nang kaunti kapag umuulan at sa peak hours.
        peak = np.isin(hour, [10, 11, 15, 16, 17]).astype(float)
        actual_wait_time = (work_ahead * rng.uniform(0.9, 1.2, n_samples)
                            + service_base_time * rng.uniform(0.95, 1.3, n_samples)
                            + precipitation * 1.0 + peak * 4)
        actual_wait_time += rng.normal(0, 4, n_samples)                    # Noise/Variance sa totoong buhay
        actual_wait_time = np.clip(actual_wait_time, 5, 480)

        X = pd.DataFrame({
            'queue_length': queue_length,
            'active_staff': active_staff,
            'precipitation': precipitation,
            'service_base_time': service_base_time,
            'work_ahead_minutes': work_ahead,
            'hour': hour,
        })[self.FEATURES]
        y = pd.Series(actual_wait_time)
        return X, y

    @staticmethod
    def _fit_new_model(X, y):
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X, y)
        return model

    def retrain_with_real_data(self, real_rows):
        """real_rows: list of dicts with the FEATURES plus actual_wait_time - see
        ai_retrain.py for how these are pulled from real completed
        bookings. Below REAL_ONLY_MIN_SAMPLES they're blended with the
        synthetic bootstrap set as a floor; from there on the model learns
        from real bookings only. Fits a NEW model, then atomically swaps it
        in (self.model = ..., a single attribute assignment) rather than
        mutating the live model in place - concurrent predict_ewt() calls
        from request threads should never see a model mid-fit.

        Returns True if retraining happened, False if there wasn't enough
        real data yet (the existing model is left untouched)."""
        if len(real_rows) < self.MIN_REAL_SAMPLES_TO_RETRAIN:
            return False

        real_df = pd.DataFrame(real_rows)
        # Rows recorded before work_ahead_minutes/hour existed: estimate the
        # work ahead as the same-service queue times the service's length,
        # and assume midday.
        if 'work_ahead_minutes' not in real_df:
            real_df['work_ahead_minutes'] = np.nan
        real_df['work_ahead_minutes'] = real_df['work_ahead_minutes'].fillna(real_df['queue_length'] * real_df['service_base_time'])
        if 'hour' not in real_df:
            real_df['hour'] = 12
        real_df['hour'] = real_df['hour'].fillna(12)
        X_real = real_df[self.FEATURES]
        y_real = real_df['actual_wait_time']

        if len(real_rows) >= self.REAL_ONLY_MIN_SAMPLES:
            X_train, y_train, trained_on = X_real, y_real, 'real'
        else:
            X_synthetic, y_synthetic = self._synthetic_training_data()
            X_train = pd.concat([X_synthetic, X_real], ignore_index=True)
            y_train = pd.concat([y_synthetic, y_real], ignore_index=True)
            trained_on = 'blended'

        self.model = self._fit_new_model(X_train, y_train)
        self.last_retrain_sample_count = len(real_rows)
        self.trained_on = trained_on
        print(f"[AI ENGINE] Retrained wait-time model on {len(real_rows)} real completed booking(s)"
              f"{'' if trained_on == 'real' else ' blended with the synthetic baseline'}.")
        return True

    def predict_ewt(self, queue_len, staff_count, rain_mm, service_minutes, work_ahead_minutes=None, hour=None):
        """
        DFD Process 2.3 - Kinukuha ang kasalukuyang operational data para mag-predict ng Estimated Wait Time.
        service_minutes: the service's real duration (services.base_duration_minutes) -
        the same feature retraining learns from. work_ahead_minutes: the work
        already ahead in the bay (wait_estimate.py); when a caller can't
        tell, it's estimated from queue_len. hour: of arrival, defaults to now.
        """
        return self.predict_many([(queue_len, service_minutes, work_ahead_minutes)], staff_count, rain_mm, hour)[0]

    def predict_many(self, cars, staff_count, rain_mm, hour=None):
        """predict_ewt for several cars at once (the live queue board) - one
        model call. cars: (queue_len, service_minutes, work_ahead_minutes)
        tuples; staffing, rainfall and hour are the same for all of them."""
        if hour is None:
            hour = datetime.now().hour
        rows = []
        for queue_len, service_minutes, work_ahead_minutes in cars:
            try:
                base_time = float(service_minutes)
            except (TypeError, ValueError):
                base_time = 0
            if base_time <= 0:
                base_time = self.DEFAULT_SERVICE_MINUTES
            if work_ahead_minutes is None:
                work_ahead_minutes = max(0, queue_len) * base_time
            # Ihanda ang data row para sa prediction logic ng scikit-learn
            rows.append({
                'queue_length': queue_len,
                'active_staff': staff_count,
                'precipitation': rain_mm,
                'service_base_time': base_time,
                'work_ahead_minutes': work_ahead_minutes,
                'hour': hour,
            })
        if not rows:
            return []
        predicted = self.model.predict(pd.DataFrame(rows)[self.FEATURES])
        return [round(max(5.0, p), 2) for p in predicted]  # Hindi pwedeng bumaba sa 5 mins ang wait time


class StaffMCDAEngine:
    def __init__(self):
        # Mga itinalagang bigat (Weights) para sa Simple Additive Weighting (SAW) Method
        # Service Speed (40%), Punctuality (40%), Customer Rating (20%) = 100%
        # Punctuality (on-time clock-ins) replaced Completion Rate on 2026-10-06:
        # a job is only assigned when someone starts it, so nearly every
        # staff member's completion rate was 100% and the criterion carried
        # 40% of the score without telling anyone apart.
        self.weights = {
            'service_speed': 0.40,
            'punctuality': 0.40,
            'customer_rating': 0.20
        }
        
    def evaluate_staff(self, staff_data_list):
        """
        DFD Process 4.0 - Kino-compute ang normalized criteria ranking para sa staff productivity matrix.
        """
        df = pd.DataFrame(staff_data_list)
        if df.empty:
            return []
            
        # Siguraduhing tugma ang mga pangalan ng column galing sa frontend / API payload
        if 'feedback' in df.columns and 'customer_rating' not in df.columns:
            df['customer_rating'] = df['feedback']
        for column in ('service_speed', 'punctuality', 'customer_rating'):
            if column not in df.columns:
                df[column] = 0

        # I-convert sa float para iwas tip error
        df['service_speed'] = pd.to_numeric(df['service_speed'], errors='coerce').fillna(0)
        df['punctuality'] = pd.to_numeric(df['punctuality'], errors='coerce').fillna(0)
        df['customer_rating'] = pd.to_numeric(df['customer_rating'], errors='coerce').fillna(0)

        # Hakbang 1: Normalization ng bawat score (Max Normalization)
        max_speed = df['service_speed'].max() if df['service_speed'].max() > 0 else 1
        max_punctuality = df['punctuality'].max() if df['punctuality'].max() > 0 else 1
        
        # Kung ang customer rating ay nasa 5.0 scale, i-normalize base doon o sa max ng listahan
        max_rating = df['customer_rating'].max() if df['customer_rating'].max() > 5 else 5.0
        if max_rating <= 0:
            max_rating = 1.0
        
        df['norm_speed'] = df['service_speed'] / max_speed
        df['norm_punctuality'] = df['punctuality'] / max_punctuality
        df['norm_rating'] = df['customer_rating'] / max_rating
        
        # Hakbang 2: Pag-timbang gamit ang SAW Formula (Score = Criteria * Weight)
        df['saw_score'] = (
            (df['norm_speed'] * self.weights['service_speed']) +
            (df['norm_punctuality'] * self.weights['punctuality']) +
            (df['norm_rating'] * self.weights['customer_rating'])
        )
        
        # Ayusin ang pagkakasunod-sunod mula Rank 1 (pinakamataas) pababa
        df['saw_score'] = df['saw_score'].round(4)
        df = df.sort_values(by='saw_score', ascending=False)
        
        # I-map pabalik ang mga kinakailangang keys para sa Frontend Table rendering
        results = []
        for index, row in enumerate(df.to_dict(orient='records')):
            results.append({
                'rank': index + 1,
                'name': row.get('name', 'Staff'),
                'service_speed': row.get('service_speed'),
                'punctuality': row.get('punctuality'),
                'feedback': row.get('customer_rating'),
                'saw_score': row.get('saw_score')
            })
            
        return results
