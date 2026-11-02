import hashlib
import os
import platform
import time
import uuid

import joblib
import numpy as np
import sklearn
from sklearn.datasets import load_iris
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def dataset():
    iris = load_iris()
    train_x, test_x, train_y, test_y = train_test_split(
        iris.data, iris.target, test_size=0.25, random_state=42, stratify=iris.target
    )
    return iris, train_x, test_x, train_y, test_y


def samples():
    iris, _, test_x, _, test_y = dataset()
    return {
        "feature_names": list(iris.feature_names),
        "class_names": list(iris.target_names),
        "samples": [{"features": x.tolist(), "label": int(y)} for x, y in zip(test_x, test_y)],
    }


def train(version, algorithm, artifact_dir, clock=time.time):
    started = time.perf_counter()
    iris, train_x, test_x, train_y, test_y = dataset()
    estimator = (
        make_pipeline(StandardScaler(), LogisticRegression(C=1, max_iter=500, random_state=42))
        if algorithm == "logistic"
        else RandomForestClassifier(n_estimators=80, max_depth=5, random_state=42, n_jobs=1)
    )
    estimator.fit(train_x, train_y)
    prediction = estimator.predict(test_x)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{version}.joblib"
    temporary = artifact_dir / f".{version}.{uuid.uuid4().hex}.tmp"
    bundle = {"model": estimator, "feature_names": list(iris.feature_names), "classes": [0, 1, 2]}
    try:
        joblib.dump(bundle, temporary, compress=3)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "version": version,
        "algorithm": algorithm,
        "artifact": path.name,
        "sha256": digest,
        "created_at": clock(),
        "dataset": "sklearn.datasets.load_iris",
        "random_seed": 42,
        "train_samples": len(train_y),
        "test_samples": len(test_y),
        "dataset_sha256": hashlib.sha256(iris.data.tobytes() + iris.target.tobytes()).hexdigest(),
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "metrics": {
            "accuracy": float(accuracy_score(test_y, prediction)),
            "f1_macro": float(f1_score(test_y, prediction, average="macro")),
            "confusion_matrix": confusion_matrix(test_y, prediction).tolist(),
        },
        "training_seconds": round(time.perf_counter() - started, 4),
    }


def load_verified(metadata, artifact_dir):
    if metadata["sklearn_version"] != sklearn.__version__:
        raise ValueError("模型的 scikit-learn 版本与当前环境不一致，请重新训练")
    root = artifact_dir.resolve()
    path = (root / metadata["artifact"]).resolve()
    if path.parent != root:
        raise ValueError("模型文件路径越界")
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
        raise ValueError("模型文件 SHA-256 校验失败")
    # Only locally trained, registered artifacts are loaded. No upload/import endpoint exists.
    bundle = joblib.load(path)
    if bundle["classes"] != [0, 1, 2] or len(bundle["feature_names"]) != 4:
        raise ValueError("模型输入输出协议不兼容")
    probe(bundle)
    return bundle


def probe(bundle):
    features = np.array([[5.1, 3.5, 1.4, 0.2], [6.2, 2.8, 4.8, 1.8]])
    prediction = bundle["model"].predict(features)
    probabilities = bundle["model"].predict_proba(features)
    if (
        prediction.shape != (2,)
        or probabilities.shape != (2, 3)
        or not np.isfinite(probabilities).all()
        or (probabilities < 0).any()
        or not np.allclose(probabilities.sum(axis=1), 1)
        or not set(prediction).issubset({0, 1, 2})
    ):
        raise ValueError("模型样例预测或概率校验失败")
