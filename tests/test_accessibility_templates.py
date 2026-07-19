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
        self.assertIn(b'dashboard.css', response.data)
        self.assertIn(b'app-role-chip', response.data)
        self.assertIn(b'Role: admin', response.data)

    def test_tasks_template_includes_skip_link(self):
        response = self.client.get("/dashboard/tasks")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'class="skip-link"', response.data)
        self.assertIn(b'href="#main-content"', response.data)

    def test_dashboard_template_uses_local_accessible_theme(self):
        response = self.client.get("/dashboard")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'dashboard.css', response.data)
        self.assertIn(b'app-role-chip', response.data)

    def test_translations_template_renders_for_audit(self):
        response = self.client.get("/translations")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'Translation Review', response.data)
        self.assertIn(b'RTL preview active', response.data)


if __name__ == "__main__":
    unittest.main()
