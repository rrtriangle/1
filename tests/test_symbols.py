from marketlab.collectors.nasdaqtrader import classify, to_yahoo


def test_yahoo_symbol_mapping():
    assert to_yahoo("BRK.B") == "BRK-B"
    assert to_yahoo("ABR$D") == "ABR-PD"
    assert to_yahoo("XYZ.WS") == "XYZ-WT"
    assert to_yahoo("AAPL") == "AAPL"


def test_security_type_rules():
    assert classify("SPDR S&P 500 ETF Trust", "Y") == ("etf", "reported")
    assert classify("Apple Inc. - Common Stock", "N")[0] == "common"
    assert classify("Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares", "N")[0] == "adr"
    assert classify("Acme Corp - Warrant", "N")[0] == "warrant"
    assert classify("Foo Acquisition Corp - Units", "N")[0] == "unit"
    assert classify("Bar Inc 6.5% Series A Cumulative Preferred Stock", "N")[0] == "preferred"


def test_mirror_symbol_normalization_matches_act_format():
    import pandas as pd
    sym = pd.Series(["ABR^D", "BRK/A", "AAPL"]).str.replace("^", "$", regex=False).str.replace("/", ".", regex=False)
    assert [to_yahoo(s) for s in sym] == ["ABR-PD", "BRK-A", "AAPL"]
