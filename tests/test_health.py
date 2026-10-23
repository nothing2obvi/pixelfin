import unittest

from app import app


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_health_is_available_without_jellyfin_setup(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()