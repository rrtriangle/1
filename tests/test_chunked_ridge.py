"""ChunkedRidge 가 sklearn Pipeline(SimpleImputer(median) → StandardScaler → Ridge) 와 같은 추정량인지 확인."""
import numpy as np
import pytest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from marketlab.research.models import ChunkedRidge


@pytest.mark.parametrize("alpha", [0.0, 10.0, 1000.0])
def test_matches_sklearn_pipeline(alpha):
    rng = np.random.default_rng(3)
    n, k = 5003, 8
    X = rng.normal(size=(n, k)).astype(np.float32) * rng.uniform(0.1, 50, k).astype(np.float32)
    X[rng.random((n, k)) < 0.1] = np.nan                  # 결측
    X[:, 5] = np.nan                                      # 전부 결측인 열 (US 시가총액처럼)
    y = np.nan_to_num(X[:, 0]) * 0.3 - np.nan_to_num(X[:, 2]) * 0.1 + rng.normal(size=n)
    core = LinearRegression() if alpha == 0 else Ridge(alpha=alpha)
    ref = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), core).fit(X.astype(np.float64), y)   # 기준은 float64 로 계산 (sklearn 은 float32 를 그대로 유지해 1e-6 수준 반올림 차이)
    mine = ChunkedRidge(alpha=alpha, chunk=777).fit(X, y)
    Xt = rng.normal(size=(300, k)).astype(np.float32)
    Xt[rng.random((300, k)) < 0.1] = np.nan
    assert np.allclose(mine.predict(Xt), ref.predict(Xt.astype(np.float64)), rtol=1e-6, atol=1e-6)
    ref_coef = np.ravel(ref[-1].coef_)
    ok = np.isfinite(ref_coef) & (np.arange(k) != 5)
    assert np.allclose(mine.coef_[ok], ref_coef[ok], rtol=1e-6, atol=1e-8)
