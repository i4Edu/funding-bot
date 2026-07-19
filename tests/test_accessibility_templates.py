import unittest

from tests.accessibility.app import app


class TemplateAccessibilityTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_dashboard_template_includes_skip_link(self):
        response = self.client.get("/dashboard")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'class="skip-link"', response.data)
        self.assertIn(b'href="#main-content"', response.data)
        self.assertIn(b'id="main-content"', response.data)

    def test_settings_template_includes_skip_link_styles(self):
        response = self.client.get("/settings")
        self.assertEqual(200, response.status_code)
        self.assertIn(b".skip-link:focus", response.data)
        self.assertIn(b'Role: admin', response.data)

    def test_tasks_template_includes_skip_link(self):
        response = self.client.get("/dashboard/tasks")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'class="skip-link"', response.data)
        self.assertIn(b'href="#main-content"', response.data)


if __name__ == "__main__":
    unittest.main()
