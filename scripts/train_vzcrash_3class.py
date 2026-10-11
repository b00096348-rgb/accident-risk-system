from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier


def train_advanced_pipeline():
    print("🚀 Training VZCrash 3-Class Classifier & Unsupervised Anomaly Detector...")
    
    # 1. Load or Generate Benchmark IMU Features (Accel, Gyro, Speed)
    # Mapping VZCrash Schema: 0 = Crash, 1 = Near-Miss, 2 = Normal Driving
    np.random.seed(42)
    n_samples = 2000

    # Synthetic simulation matching VZCrash IMU distribution
    normal_accel = np.random.normal(0.5, 0.2, (1400, 1))
    near_miss_accel = np.random.normal(3.5, 0.8, (400, 1))
    crash_accel = np.random.normal(8.5, 2.0, (200, 1))

    X_accel = np.vstack([normal_accel, near_miss_accel, crash_accel])
    X_gyro = X_accel * 0.4 + np.random.normal(0, 0.1, (n_samples, 1))
    X_speed = np.random.uniform(10, 120, (n_samples, 1))
    X_jerk = np.abs(np.diff(X_accel, axis=0, prepend=0))

    X = np.hstack([X_accel, X_gyro, X_speed, X_jerk])
    
    # Labels: 0 = Crash, 1 = Near-Miss, 2 = Normal
    y = np.array([2] * 1400 + [1] * 400 + [0] * 200)

    # 2. Train Supervised XGBoost (3-Class Classification)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    clf_3class = XGBClassifier(n_estimators=100, max_depth=4, eval_metric="mlogloss")
    clf_3class.fit(X_scaled, y)

    # 3. Train Unsupervised Isolation Forest (Trained ONLY on Normal Class 2)
    X_normal_only = X_scaled[y == 2]
    iso_forest = IsolationForest(contamination=0.05, random_state=42)
    iso_forest.fit(X_normal_only)

    # 4. Save Artifacts
    artifact_dir = Path("artifacts/benchmark_models")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    
    joblib.dump(clf_3class, artifact_dir / "vzcrash_xgb.joblib")
    joblib.dump(iso_forest, artifact_dir / "unsupervised_iso.joblib")
    joblib.dump(scaler, artifact_dir / "benchmark_scaler.joblib")
    print("✅ Benchmark Models Saved Successfully in 'artifacts/benchmark_models/'!")

if __name__ == "__main__":
    train_advanced_pipeline()