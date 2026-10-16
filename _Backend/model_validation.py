"""Chronological real-only holdout: finish training jobs before test arrivals."""
from datetime import datetime
import hashlib
import json
import math

import numpy as np
import pandas as pd
from database import get_db_connection
from ai_engine import WaitTimePredictorAI


def evaluate(rows):
    rows=sorted((r for r in rows if r.get('created_at') and r.get('completed_at')),
                key=lambda r:(r['created_at'],r.get('reservation_id',0)))
    if len(rows)<30: return None
    boundary=max(1,int(len(rows)*0.8))
    test=rows[boundary:]
    train=[r for r in rows[:boundary] if r['completed_at']<test[0]['created_at']]
    if len(train)<20 or len(test)<5: return None
    fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
    conn=get_db_connection()
    try:
        if conn.execute('SELECT 1 FROM model_evaluations WHERE fingerprint=?',(fingerprint,)).fetchone(): return None
    finally: conn.close()
    def frame(values):
        data=pd.DataFrame(values)
        data['work_ahead_minutes']=data['work_ahead_minutes'].fillna(data['queue_length']*data['service_base_time'])
        data['hour']=data['hour'].fillna(12)
        return data
    train_df,test_df=frame(train),frame(test)
    model=WaitTimePredictorAI._fit_new_model(train_df[WaitTimePredictorAI.FEATURES],train_df['actual_wait_time'])
    errors=model.predict(test_df[WaitTimePredictorAI.FEATURES])-test_df['actual_wait_time'].to_numpy()
    result={'fingerprint':fingerprint,'evaluated_at':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'train_samples':len(train),'test_samples':len(test),
            'train_until':max(r['completed_at'] for r in train),'test_from':test[0]['created_at'],
            'test_until':max(r['completed_at'] for r in test),
            'mae':round(float(np.mean(np.abs(errors))),2),
            'rmse':round(float(math.sqrt(np.mean(errors**2))),2),'bias':round(float(np.mean(errors)),2)}
    conn=get_db_connection()
    try:
        columns=','.join(result)
        conn.execute(f'INSERT OR IGNORE INTO model_evaluations({columns}) VALUES ({",".join("?" for _ in result)})',list(result.values()))
        conn.commit()
    finally: conn.close()
    return result
