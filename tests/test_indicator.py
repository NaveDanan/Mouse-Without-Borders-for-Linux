import unittest
from pathlib import Path
from unittest.mock import Mock

from mwb_linux.indicator import MENU_PATH, TopBarIndicator, menu_layout, menu_properties


class IndicatorMenuTests(unittest.TestCase):
    def setUp(self):
        self.open = Mock()
        self.settings = Mock()
        self.exit = Mock()
        self.indicator = TopBarIndicator(
            icon_name="io.github.NaveDanan.MouseWithoutBorders",
            icon_theme_path=Path("/tmp/icons"),
            on_open=self.open,
            on_settings=self.settings,
            on_exit=self.exit,
        )

    def test_menu_layout_contains_exactly_the_requested_actions(self):
        revision, root = menu_layout().unpack()

        self.assertEqual(revision, 1)
        self.assertEqual(root[0], 0)
        self.assertEqual(
            [(child[0], child[1]["label"]) for child in root[2]],
            [(1, "Open"), (2, "Settings"), (3, "Exit UI (keep sharing)")],
        )

    def test_menu_properties_honor_the_requested_filter(self):
        properties = menu_properties(1, ["label"])

        self.assertEqual(list(properties), ["label"])
        self.assertEqual(properties["label"].unpack(), "Open")

    def test_each_clicked_menu_item_runs_its_matching_action(self):
        for item_id in (1, 2, 3):
            self.assertTrue(self.indicator.activate_menu_item(item_id))

        self.open.assert_called_once_with()
        self.settings.assert_called_once_with()
        self.exit.assert_called_once_with()

    def test_unknown_items_and_non_activation_events_are_ignored(self):
        self.assertFalse(self.indicator.activate_menu_item(99))
        self.assertFalse(self.indicator.activate_menu_item(1, "hovered"))
        self.open.assert_not_called()

    def test_status_notifier_properties_expose_icon_and_menu(self):
        get_property = self.indicator._on_item_property

        self.assertEqual(get_property(None, "", "", "", "Status").unpack(), "Active")
        self.assertEqual(get_property(None, "", "", "", "Menu").unpack(), MENU_PATH)
        self.assertEqual(
            get_property(None, "", "", "", "IconThemePath").unpack(),
            "/tmp/icons",
        )


if __name__ == "__main__":
    unittest.main()
