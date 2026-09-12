from app.fork.jobs.inbound_usage import _process_inbounds_stats_response
from app.fork.jobs.usage_coeff import apply_usage_value


class _Stat:
    def __init__(self, value, type, name=None, link=None):
        self.value = value
        self.type = type
        self.name = name
        self.link = link


class _Resp:
    def __init__(self, stats):
        self.stats = stats


def test_inbounds_fold_up_and_down_by_tag():
    resp = _Resp(
        [
            _Stat(10, "uplink", name="vless"),
            _Stat(4, "downlink", name="vless"),
            _Stat(7, "uplink", link="tcp"),
            _Stat(0, "uplink", name="skip"),
        ]
    )
    assert _process_inbounds_stats_response(resp) == {
        "vless": {"up": 10, "down": 4},
        "tcp": {"up": 7, "down": 0},
    }


def test_apply_usage_value_rounds_split_coefficients():
    assert apply_usage_value(5, 1.5) == 8
    assert apply_usage_value(1, 0.5) == 0
    assert apply_usage_value(1, 0.6) == 1
