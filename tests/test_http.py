import unittest

from calsync.http import HttpError


class HttpErrorTests(unittest.TestCase):
    def test_google_string_error_code(self):
        err = HttpError(400, {"error": "invalid_grant"}, "u")
        self.assertEqual(err.error_code, "invalid_grant")

    def test_graph_nested_error_code(self):
        err = HttpError(400, {"error": {"code": "authorization_pending"}}, "u")
        self.assertEqual(err.error_code, "authorization_pending")

    def test_no_code(self):
        self.assertIsNone(HttpError(500, "boom", "u").error_code)


if __name__ == "__main__":
    unittest.main()
