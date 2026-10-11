from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier
from xgboost import XGBClassifier


def train_and_evaluate_fusion():
    obd_path = Path("data/raw/obd_entries.csv")
    whoop_path = Path("data/raw/whoop_ble_30s_vitals.csv")

    if not obd_path.exists() or not whoop_path.exists():
        print("\n❌ Missing raw data files in data/raw/!")
        print("Please run scripts/test_obd.py and scripts/stream_whoop_ble_csv.py first to collect CSV data.\n")
        return

    # 1. Load CSVs
    df_obd = pd.read_csv(obd_path)
    df_whoop = pd.read_csv(whoop_path)

    if df_obd.empty or df_whoop.empty:
        print("\n❌ One or both dataset CSVs in data/raw/ are empty.\n")
        return

    # Convert timestamps
    df_obd["datetime"] = pd.to_datetime(df_obd["timestamp"])
    df_whoop["datetime"] = pd.to_datetime(df_whoop["datetime"])

    # 2. Synchronize OBD (~5Hz) and WHOOP (30s windows) via backward nearest-neighbor join
    df_merged = (
        pd.merge_asof(
            df_obd.sort_values("datetime"),
            df_whoop.sort_values("datetime"),
            on="datetime",
            direction="backward",
        )
        .bfill()
        .fillna(0)
    )

    # 3. Define Combined Feature Set
    feature_cols = [
        "speed",
        "rpm",
        "throttle_position",
        "engine_load_value",
        "acceleration",
        "mean_hr_30s",
        "rmssd_30s",
        "sdnn_30s",
        "pnn50_30s",
    ]

    missing_cols = [
        col for col in feature_cols if col not in df_merged.columns]
    if missing_cols:
        print(
            f"\n❌ Missing expected feature columns in merged data: {missing_cols}\n")
        return

    X = df_merged[feature_cols]

    # Target variable: High acceleration/speed or physiological distress (HRV drop)
    y = np.where(
        (X["acceleration"] > 2.5) | (X["speed"] > 100) | (
            X["rmssd_30s"] < 20.0), 1, 0
    )

    # 4. Train / Test Split
    if len(np.unique(y)) < 2:
        print("⚠️ Notice: Logged dataset contains only one class label. Splitting without stratification...")
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=42
        )
    else:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=42, stratify=y
        )

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # 5. Candidate Machine Learning Algorithms
    algorithms = {
        "Logistic Regression": LogisticRegression(random_state=42),
        "Decision Tree": DecisionTreeClassifier(random_state=42, max_depth=5),
        "Random Forest": RandomForestClassifier(n_estimators=100, random_state=42),
        "Support Vector Machine": SVC(probability=True, random_state=42),
        "XGBoost": XGBClassifier(n_estimators=100, max_depth=4, random_state=42),
    }

    results = []
    best_model = None
    best_score = -1.0
    best_model_name = ""

    print("\n" + "=" * 65)
    print(" 🚀 MODEL COMPARISON & EVALUATION BENCHMARK ")
    print("=" * 65)

    # 6. Benchmark Models
    for name, model in algorithms.items():
        model.fit(X_train_scaled, y_train)
        y_pred = model.predict(X_test_scaled)
        y_prob = model.predict_proba(X_test_scaled)[:, 1]

        acc = accuracy_score(y_test, y_pred)
        prec = precision_score(y_test, y_pred, zero_division=0)
        rec = recall_score(y_test, y_pred, zero_division=0)
        f1 = f1_score(y_test, y_pred, zero_division=0)
        auc = (
            roc_auc_score(y_test, y_prob) if len(
                np.unique(y_test)) > 1 else 0.0
        )

        results.append(
            {
                "Algorithm": name,
                "Accuracy": round(acc, 4),
                "Precision": round(prec, 4),
                "Recall": round(rec, 4),
                "F1 Score": round(f1, 4),
                "ROC AUC": round(auc, 4),
            }
        )

        if f1 >= best_score:
            best_score = f1
            best_model = model
            best_model_name = name

    results_df = pd.DataFrame(results)
    print(results_df.to_string(index=False))
    print("=" * 65)
    print(
        f"🏆 Selected Best Model: {best_model_name} (F1 Score: {best_score:.4f})")

    # 7. Export Best Artifacts & Metrics
    artifact_dir = Path("artifacts/fusion_model")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(best_model, artifact_dir / "fusion_model.joblib")
    joblib.dump(scaler, artifact_dir / "scaler.joblib")
    results_df.to_csv(
        artifact_dir / "model_comparison_results.csv", index=False)
    print(f"✅ Saved best model to {artifact_dir / 'fusion_model.joblib'}\n")


if __name__ == "__main__":
    train_and_evaluate_fusion()
