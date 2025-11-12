import os
import numpy as np
import tensorflow as tf
from datetime import datetime
from django.conf import settings
from .models import CollectionHistory, CollectionSchedule, Route
from .model_loader import ensure_model_available

WEATHER_MAP = {'sunny': 2, 'cloudy': 1, 'rainy': 0}
TRAFFIC_MAP = {'light': 2, 'moderate': 1, 'heavy': 0}

def _minutes(t):
    return t.hour * 60 + t.minute

def _day_of_year(d):
    return int(d.strftime('%j'))

def _sin_cos_day(d):
    doy = _day_of_year(d)
    return np.sin(2*np.pi*doy/365.0), np.cos(2*np.pi*doy/365.0)

def _one_hot(i, n):
    a = np.zeros(n, dtype=np.float32)
    if 0 <= i < n:
        a[i] = 1.0
    return a

class TensorFlowArrivalPredictor:
    def __init__(self):
        self.model_path = os.path.join(settings.BASE_DIR, 'models', 'arrival_tf.h5')
        self.model = None

    def build_model(self, input_dim):
        inp = tf.keras.Input(shape=(input_dim,))
        x = tf.keras.layers.Dense(128, activation='relu')(inp)
        x = tf.keras.layers.Dense(64, activation='relu')(x)
        x = tf.keras.layers.Dropout(0.2)(x)
        out = tf.keras.layers.Dense(2)(x)
        m = tf.keras.Model(inp, out)
        m.compile(optimizer='adam', loss='mse', metrics=['mae'])
        return m

    def _row_features(self, rec: CollectionHistory):
        dow = rec.date.weekday()
        dow_oh = _one_hot(dow, 7)
        sin_doy, cos_doy = _sin_cos_day(rec.date)
        sched = CollectionSchedule.objects.filter(route=rec.route, day_of_week=dow, is_active=True).first()
        sched_start = _minutes(sched.start_time) if sched else 0
        sched_end = _minutes(sched.end_time) if sched else 0
        wx = WEATHER_MAP.get(str(rec.weather_condition or '').lower(), 1)
        tr = TRAFFIC_MAP.get(str(rec.traffic_condition or '').lower(), 1)
        start_m = _minutes(rec.start_time)
        dur_m = (_minutes(rec.end_time) - start_m) if rec.end_time else 120
        return np.array([*dow_oh, sin_doy, cos_doy, float(sched_start), float(sched_end), float(wx), float(tr), float(start_m), float(dur_m)], dtype=np.float32)

    def _gen(self, route_id=None):
        qs = CollectionHistory.objects.all()
        if route_id:
            qs = qs.filter(route_id=route_id)
        for rec in qs.iterator():
            X = self._row_features(rec)
            sm = _minutes(rec.start_time)
            dm = (_minutes(rec.end_time) - sm) if rec.end_time else 120
            y = np.array([float(sm), float(dm)], dtype=np.float32)
            yield X, y

    def _dataset(self, route_id=None, batch_size=1024, shuffle_buffer=10000):
        spec_x = tf.TensorSpec(shape=(15,), dtype=tf.float32)
        spec_y = tf.TensorSpec(shape=(2,), dtype=tf.float32)
        ds = tf.data.Dataset.from_generator(lambda: self._gen(route_id), output_signature=(spec_x, spec_y))
        return ds.shuffle(shuffle_buffer).batch(batch_size).prefetch(tf.data.AUTOTUNE)

    def train(self, route_id=None, epochs=8, batch_size=1024):
        ds = self._dataset(route_id, batch_size=batch_size)
        self.model = self.build_model(15)
        self.model.fit(ds, epochs=epochs, verbose=1)
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        self.model.save(self.model_path)
        return True

    def load(self):
        local_path = ensure_model_available()
        if local_path:
            self.model_path = local_path
        if os.path.exists(self.model_path):
            self.model = tf.keras.models.load_model(self.model_path)
            return True
        return False

    def _features_for_date(self, route_id, target_date, context=None):
        context = context or {}
        dow = target_date.weekday()
        dow_oh = _one_hot(dow, 7)
        sin_doy, cos_doy = _sin_cos_day(target_date)
        route = Route.objects.get(id=route_id)
        sched = CollectionSchedule.objects.filter(route=route, day_of_week=dow, is_active=True).first()
        sched_start = _minutes(sched.start_time) if sched else 0
        sched_end = _minutes(sched.end_time) if sched else 0
        wx = WEATHER_MAP.get(str(context.get('weather', 'cloudy')).lower(), 1)
        tr = TRAFFIC_MAP.get(str(context.get('traffic', 'moderate')).lower(), 1)
        return np.array([*dow_oh, sin_doy, cos_doy, float(sched_start), float(sched_end), float(wx), float(tr), 0.0, 0.0], dtype=np.float32)

    def predict_route_schedule(self, route_id, target_date, context=None):
        if self.model is None and not self.load():
            return None
        X = self._features_for_date(route_id, target_date, context or {})
        pred = self.model.predict(np.expand_dims(X, axis=0), verbose=0)[0]
        start_min = float(pred[0])
        duration = max(30.0, float(pred[1]))
        sh = int(start_min // 60) % 24
        sm = int(start_min % 60)
        em_total = start_min + duration
        eh = int(em_total // 60) % 24
        em = int(em_total % 60)
        return {
            'predicted_start_time': datetime.strptime(f"{sh}:{sm}", "%H:%M").time(),
            'predicted_end_time': datetime.strptime(f"{eh}:{em}", "%H:%M").time(),
            'confidence_score': 0.6,
            'factors': {'tf_model': True}
        }