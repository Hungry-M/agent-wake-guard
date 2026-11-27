from pathlib import Path
from string import Formatter
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from guard import initialize_language, taskbar_message
from i18n import ZH_HANS, current_language, select_language, tr
from test_guard import sample


class LanguageTests(TestCase):
    def setUp(self):
        self.addCleanup(select_language, current_language())

    def test_auto_uses_windows_ui_language(self):
        for windows, expected in ((0x0804, "zh-Hans"), (0x1004, "zh-Hans"), (0x0409, "en"), (0x0404, "en")):
            with self.subTest(windows=windows), patch("i18n.ui_language", return_value=windows):
                select_language()
                self.assertEqual(current_language(), expected)

    def test_config_and_override_use_the_same_ui_catalog(self):
        with TemporaryDirectory() as temp:
            config = Path(temp) / "config.ini"
            config.write_text("[guard]\nlanguage = zh-Hans\n", encoding="utf-8")
            initialize_language(config, None)
            self.assertEqual(tr("Switch to tray"), "切换为托盘")
            self.assertTrue(taskbar_message(sample(), 7, "hibernate")[0].endswith("7 秒后休眠"))
            initialize_language(config, "en")
            self.assertEqual(tr("Switch to tray"), "Switch to tray")
            self.assertTrue(taskbar_message(sample(), 7, "hibernate")[0].endswith("Hibernate in 7s"))

    def test_catalog_preserves_format_fields(self):
        formatter = Formatter()
        for source, localized in ZH_HANS.items():
            expected = {field for _, field, _, _ in formatter.parse(source) if field is not None}
            actual = {field for _, field, _, _ in formatter.parse(localized) if field is not None}
            self.assertEqual(expected, actual, source)
