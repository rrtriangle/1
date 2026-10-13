import pytest

pytestmark = pytest.mark.network


def test_marcap_remote_listing():
    from marketlab.collectors import marcap
    from marketlab.net.http import HttpClient
    try:
        commit, remote, method = marcap.list_remote(HttpClient("marcap"))
    except Exception as e:
        pytest.skip(f"오프라인: {e}")
    assert 1995 in remote and len(remote) >= 30
