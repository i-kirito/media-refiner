import unittest

from app.routers.gaps import (
    GapSubscriptionStatusItem,
    _gap_subscription_status_rows,
    _mp_subscription_tmdb_id,
)


class GapSubscriptionStatusTests(unittest.TestCase):
    def test_extracts_tmdb_id_from_supported_moviepilot_fields(self):
        self.assertEqual(194583, _mp_subscription_tmdb_id({"tmdbid": 194583}))
        self.assertEqual(
            136311,
            _mp_subscription_tmdb_id({"media_source": "themoviedb", "media_id": "136311"}),
        )
        self.assertEqual(57532, _mp_subscription_tmdb_id({"mediaid": "tmdb:57532"}))

    def test_marks_all_target_seasons_as_subscribed(self):
        items = [
            GapSubscriptionStatusItem(
                series_id="594124",
                series_name="行尸走肉：死亡之城",
                tmdb_id=194583,
                seasons=[3],
            )
        ]
        subscriptions = [{"tmdbid": 194583, "season": 3, "id": 1857}]

        rows = _gap_subscription_status_rows(items, subscriptions)

        self.assertEqual("subscribed", rows[0]["status"])
        self.assertEqual([3], rows[0]["subscribed_seasons"])

    def test_marks_only_some_target_seasons_as_partial(self):
        items = [
            GapSubscriptionStatusItem(
                series_id="series-1",
                series_name="测试剧",
                tmdb_id=12345,
                seasons=[1, 2],
            )
        ]
        subscriptions = [{"media_source": "themoviedb", "media_id": "12345", "season": 2}]

        rows = _gap_subscription_status_rows(items, subscriptions)

        self.assertEqual("partial", rows[0]["status"])
        self.assertEqual([2], rows[0]["subscribed_seasons"])

    def test_does_not_match_a_different_season(self):
        items = [
            GapSubscriptionStatusItem(
                series_id="series-2",
                series_name="测试剧",
                tmdb_id=12345,
                seasons=[3],
            )
        ]
        subscriptions = [{"tmdbid": 12345, "season": 2}]

        rows = _gap_subscription_status_rows(items, subscriptions)

        self.assertEqual("none", rows[0]["status"])
        self.assertEqual([], rows[0]["subscribed_seasons"])


if __name__ == "__main__":
    unittest.main()
