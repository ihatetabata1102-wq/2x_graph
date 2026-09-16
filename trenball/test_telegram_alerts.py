from trenball.telegram_alerts import AlertRule, count_10x_in_recent_points, points_since_last_10x


def test_alert_matches_expected_value_with_tolerance() -> None:
    rule = AlertRule(name="Upper band", enabled=True, expected_value=10, tolerance=3)
    assert rule.matches(1, 7)
    assert rule.matches(500, 13)
    assert not rule.matches(1, 6)


def test_specific_step_range_is_inclusive() -> None:
    rule = AlertRule(enabled=True, expected_value=0, tolerance=0, unlimited=False, start_step=10, end_step=20)
    assert not rule.matches(9, 0)
    assert rule.matches(10, 0)
    assert rule.matches(20, 0)
    assert not rule.matches(21, 0)


def test_rule_round_trip_has_no_credentials() -> None:
    rule = AlertRule.from_dict({"name": "Low", "enabled": True, "expected_value": -20})
    assert rule.name == "Low"
    assert "bot_token" not in rule.to_dict()
    assert "chat_id" not in rule.to_dict()


def test_points_since_last_10x() -> None:
    assert points_since_last_10x(["start"] + ["2x+"] * 50) == 50
    assert points_since_last_10x(["start", "2x-", "10x", "2x+", "2x-"]) == 2
    assert points_since_last_10x(["start", "10x"]) == 0


def test_count_10x_in_recent_window() -> None:
    events = ["start", "10x"] + ["2x+"] * 49
    assert count_10x_in_recent_points(events, 50) == (1, 50)
    events.extend(["10x", "10x"])
    assert count_10x_in_recent_points(events, 50) == (2, 50)
