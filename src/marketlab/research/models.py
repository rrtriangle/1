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
        elif a in ("linear", "ridge", "logistic"):
            from sklearn.impute import SimpleImputer
            from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
            core = {"linear": LinearRegression(), "ridge": Ridge(alpha=self.params.get("alpha", 10.0)),
                    "logistic": LogisticRegression(C=self.params.get("C", 0.1), max_iter=500)}[a]
            self.est = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), core)
            Xc = X[self.features].clip(-1e6, 1e6)
            self.est.fit(Xc, y)
            self.backend = "sklearn"
        elif a == "lgbm":
            self._fit_gbm(X, y, X_val, y_val, seed)
        elif a == "rf":
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.impute import SimpleImputer
            from sklearn.pipeline import make_pipeline
            self.est = make_pipeline(SimpleImputer(strategy="median"), RandomForestRegressor(
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
        Xf = X[self.features].clip(-1e6, 1e6) if a in ("linear", "ridge") else X[self.features]
        return np.asarray(self.est.predict(Xf), dtype=np.float64)

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
            coef = np.ravel(self.est[-1].coef_)
            return pd.Series(np.abs(coef) / max(np.abs(coef).sum(), 1e-12), index=self.features).sort_values(ascending=False)
        return None

    def coef_sign(self) -> pd.Series | None:
        if self.algorithm in ("linear", "ridge", "logistic"):
            return pd.Series(np.sign(np.ravel(self.est[-1].coef_)), index=self.features)
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
