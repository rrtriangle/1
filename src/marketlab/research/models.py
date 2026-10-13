"""모델 모음: 비교 기준 모델 + 선형 모델 + LightGBM (+RandomForest). 모두 CPU, 스레드 2.

전처리(결측 대체·표준화)는 학습 데이터로만 적합하고 검증·테스트에는 같은 변환만 적용한다(sklearn Pipeline).
LightGBM 을 불러올 수 없으면(macOS 에서 libomp 미설치 등) sklearn HistGradientBoosting 으로 대체하고 그 사실을 기록한다.
"""
from __future__ import annotations

import pickle
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .resources import threads

warnings.filterwarnings("ignore", message=".*eval_set.*deprecated.*")

REGRESSION_ALGOS = ["zero", "hist_mean", "momentum", "reversal", "linear", "ridge", "lgbm", "rf"]
CLASSIFIER_ALGOS = ["always_up", "momentum", "logistic", "lgbm"]


def _lgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
        return True
    except Exception:
        return False


class ChunkedRidge:
    """메모리 절약형 Ridge/선형회귀 (sklearn Pipeline(SimpleImputer(median) → StandardScaler → Ridge) 와 같은 추정량).

    sklearn 파이프라인은 학습 행렬의 float64 사본을 여러 개 만들어 150만 행 × 62 특징에서 4.5GB 까지 치솟는다(실측).
    여기서는 학습 구간 중앙값으로 결측을 채우고 학습 구간 평균·표준편차로 표준화한 뒤, 20만 행 단위로
    ZᵀZ·Zᵀy 를 누적해 (ZᵀZ + αI)w = Zᵀ(y−ȳ) 를 푼다. 절편은 벌점 없이 ȳ (표준화된 Z 의 평균이 0 이므로).
    """

    def __init__(self, alpha: float = 10.0, chunk: int = 200_000):
        self.alpha, self.chunk = float(alpha), int(chunk)

    def _z(self, X: np.ndarray) -> np.ndarray:
        Z = np.where(np.isnan(X), self.median_, X).astype(np.float64)
        Z -= self.mean_
        Z /= self.scale_
        return Z

    def fit(self, X, y):
        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)          # 전부 결측인 열
            med = np.nanmedian(X, axis=0)
        self.median_ = np.where(np.isnan(med), 0.0, med)          # 전부 결측인 열은 0 (keep_empty_features 와 동일)
        n, k = X.shape
        fill = lambda c: np.where(np.isnan(c), self.median_, c).astype(np.float64)
        s1 = np.zeros(k)
        for i in range(0, n, self.chunk):
            s1 += fill(X[i:i + self.chunk]).sum(0)
        self.mean_ = s1 / n
        s2 = np.zeros(k)                                           # 2-pass 분산 (s2/n − 평균² 의 자릿수 손실 방지)
        for i in range(0, n, self.chunk):
            s2 += ((fill(X[i:i + self.chunk]) - self.mean_) ** 2).sum(0)
        var = s2 / n
        self.scale_ = np.where(var > 1e-24, np.sqrt(var), 1.0)
        ym = y.mean()
        A = np.zeros((k, k))
        b = np.zeros(k)
        for i in range(0, n, self.chunk):
            Z = self._z(X[i:i + self.chunk])
            A += Z.T @ Z
            b += Z.T @ (y[i:i + self.chunk] - ym)
        self.coef_ = np.linalg.solve(A + max(self.alpha, 1e-8) * np.eye(k), b)
        self.intercept_ = ym
        return self

    def predict(self, X) -> np.ndarray:
        X = np.asarray(X, dtype=np.float32)
        out = np.empty(len(X))
        for i in range(0, len(X), self.chunk):
            out[i:i + self.chunk] = self._z(X[i:i + self.chunk]) @ self.coef_ + self.intercept_
        return out


@dataclass
class Model:
    algorithm: str
    task: str                      # regression | classification
    features: list[str]
    params: dict = field(default_factory=dict)
    est: object = None
    const: float = 0.0
    calibrator: object = None
    backend: str = ""
    best_iteration: int | None = None

    # ------------------------------------------------------------------ fit
    def fit(self, X: pd.DataFrame, y: np.ndarray, X_val: pd.DataFrame | None = None, y_val: np.ndarray | None = None,
            seed: int = 42) -> "Model":
        a = self.algorithm
        if a in ("zero",):
            self.const = 0.0
        elif a in ("hist_mean", "always_up"):
            self.const = float(np.nanmean(y))
        elif a in ("momentum", "reversal"):
            pass
        elif a in ("linear", "ridge"):
            self.est = ChunkedRidge(alpha=0.0 if a == "linear" else self.params.get("alpha", 10.0))
            self.est.fit(X[self.features].clip(-1e6, 1e6).to_numpy(np.float32), y)
            self.backend = "chunked_ridge"
        elif a == "logistic":
            from sklearn.impute import SimpleImputer
            from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
            core = {"linear": LinearRegression(), "ridge": Ridge(alpha=self.params.get("alpha", 10.0)),
                    "logistic": LogisticRegression(C=self.params.get("C", 0.1), max_iter=500)}[a]
            self.est = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), core)
            Xc = X[self.features].clip(-1e6, 1e6)
            self.est.fit(Xc, y)
            self.backend = "sklearn"
        elif a == "lgbm":
            self._fit_gbm(X, y, X_val, y_val, seed)
        elif a == "rf":
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.impute import SimpleImputer
            from sklearn.pipeline import make_pipeline
            self.est = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), RandomForestRegressor(
                n_estimators=self.params.get("n_estimators", 150), max_depth=self.params.get("max_depth", 8),
                min_samples_leaf=self.params.get("min_samples_leaf", 200), max_samples=min(0.3, 300_000 / max(1, len(X))),
                n_jobs=threads(), random_state=seed))
            self.est.fit(X[self.features], y)
            self.backend = "sklearn_rf"
        else:
            raise ValueError(f"알 수 없는 알고리즘: {a}")
        return self

    def _fit_gbm(self, X, y, X_val, y_val, seed):
        from ..config import get_settings
        p = dict(get_settings().get("research.lightgbm", {}) or {})
        p.update(self.params)
        es = int(p.pop("early_stopping_rounds", 50))
        if _lgbm_available():
            import lightgbm as lgb
            cls = lgb.LGBMClassifier if self.task == "classification" else lgb.LGBMRegressor
            self.est = cls(**p, n_jobs=threads(), random_state=seed, verbose=-1)
            kw = {}
            if X_val is not None and len(X_val):
                kw = {"eval_set": [(X_val[self.features], y_val)], "callbacks": [lgb.early_stopping(es, verbose=False)]}
            self.est.fit(X[self.features], y, **kw)
            self.best_iteration = getattr(self.est, "best_iteration_", None)
            self.backend = f"lightgbm {lgb.__version__}"
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
            cls = HistGradientBoostingClassifier if self.task == "classification" else HistGradientBoostingRegressor
            self.est = cls(max_iter=p.get("n_estimators", 600), learning_rate=p.get("learning_rate", 0.03),
                           max_leaf_nodes=p.get("num_leaves", 31), min_samples_leaf=p.get("min_child_samples", 200),
                           early_stopping=True, validation_fraction=0.15, random_state=seed)
            self.est.fit(X[self.features], y)
            self.backend = "sklearn HistGradientBoosting (LightGBM 불가 → 대체)"

    # ------------------------------------------------------------------ predict
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        a = self.algorithm
        n = len(X)
        if a in ("zero", "hist_mean"):
            return np.full(n, self.const, dtype=np.float64)
        if a == "always_up":
            return np.full(n, self.const, dtype=np.float64)
        if a == "momentum":
            v = X["ret_60"].to_numpy(dtype=np.float64)
            if self.task == "classification":
                return np.where(np.isnan(v), 0.5, 1 / (1 + np.exp(-v * 5)))
            return np.nan_to_num(v, nan=0.0) * 0.05   # 순위 정보만 의미 (크기는 임의 축척)
        if a == "reversal":
            return -np.nan_to_num(X["ret_5"].to_numpy(dtype=np.float64), nan=0.0) * 0.1
        if self.task == "classification":
            if a == "logistic":
                p = self.est.predict_proba(X[self.features].clip(-1e6, 1e6))[:, 1]
            else:
                p = self.est.predict_proba(X[self.features])[:, 1]
            if self.calibrator is not None:
                p = self.calibrator.predict(p)
            return p
        if a in ("linear", "ridge"):
            return self.est.predict(X[self.features].clip(-1e6, 1e6).to_numpy(np.float32))
        return np.asarray(self.est.predict(X[self.features]), dtype=np.float64)

    def calibrate(self, p_val_raw: np.ndarray, y_val: np.ndarray) -> None:
        """검증 구간 관측 결과로 확률 보정 (등위회귀). 학습 구간은 쓰지 않는다."""
        from sklearn.isotonic import IsotonicRegression
        ir = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        ir.fit(p_val_raw, y_val)
        self.calibrator = ir

    def contributions(self, X: pd.DataFrame, max_rows: int = 20000) -> pd.DataFrame | None:
        """SHAP 값 (LightGBM pred_contrib). 다른 모델은 None."""
        if self.algorithm != "lgbm" or not self.backend.startswith("lightgbm"):
            return None
        Xs = X[self.features].iloc[:max_rows]
        c = self.est.booster_.predict(Xs, pred_contrib=True)
        if isinstance(c, list):
            c = c[-1]
        return pd.DataFrame(c[:, :-1], columns=self.features, index=Xs.index)

    def importance(self) -> pd.Series | None:
        if self.algorithm == "lgbm" and hasattr(self.est, "booster_"):
            g = self.est.booster_.feature_importance(importance_type="gain")
            return pd.Series(g / max(g.sum(), 1e-12), index=self.features).sort_values(ascending=False)
        if self.algorithm in ("linear", "ridge", "logistic"):
            coef = np.ravel(self.est.coef_ if hasattr(self.est, "coef_") else self.est[-1].coef_)
            return pd.Series(np.abs(coef) / max(np.abs(coef).sum(), 1e-12), index=self.features).sort_values(ascending=False)
        return None

    def coef_sign(self) -> pd.Series | None:
        if self.algorithm in ("linear", "ridge", "logistic"):
            coef = self.est.coef_ if hasattr(self.est, "coef_") else self.est[-1].coef_
            return pd.Series(np.sign(np.ravel(coef)), index=self.features)
        return None

    # ------------------------------------------------------------------ io
    def save(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        return path.stat().st_size

    @staticmethod
    def load(path: Path) -> "Model":
        with open(path, "rb") as fh:
            return pickle.load(fh)
