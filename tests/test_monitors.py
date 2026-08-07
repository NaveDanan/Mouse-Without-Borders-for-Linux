import unittest

from mwb_linux.monitors import (
    Monitor,
    arrange,
    default_host_cell,
    is_exterior,
    normalize,
    resolve_placement,
)

LAPTOP = Monitor(name="eDP-1", x=0, y=0, width=1920, height=1080)
EXTERNAL = Monitor(name="DP-2", x=1920, y=0, width=2560, height=1440)
ABOVE = Monitor(name="HDMI-1", x=0, y=-1080, width=1920, height=1080)


class MonitorMatrixTests(unittest.TestCase):
    def test_side_by_side_monitors_share_a_row(self):
        self.assertEqual(arrange([EXTERNAL, LAPTOP]), [(0, 1), (0, 0)])

    def test_stacked_monitors_use_separate_rows(self):
        self.assertEqual(arrange([LAPTOP, ABOVE]), [(1, 0), (0, 0)])

    def test_host_starts_next_to_the_monitor_it_hands_over_to(self):
        monitors = [LAPTOP, EXTERNAL]
        cells = arrange(monitors)
        self.assertEqual(default_host_cell(monitors, cells, "right", EXTERNAL.zone), (0, 2))
        self.assertEqual(default_host_cell(monitors, cells, "left", LAPTOP.zone), (0, -1))
        self.assertEqual(default_host_cell(monitors, cells, "top", LAPTOP.zone), (-1, 0))

    def test_unknown_zone_falls_back_to_the_outermost_monitor(self):
        monitors = [LAPTOP, EXTERNAL]
        cells = arrange(monitors)
        self.assertEqual(default_host_cell(monitors, cells, "right", []), (0, 2))
        self.assertEqual(default_host_cell(monitors, cells, "left", []), (0, -1))

    def test_placement_names_the_edge_and_the_monitor(self):
        monitors = [LAPTOP, EXTERNAL]
        cells = arrange(monitors)
        self.assertEqual(resolve_placement(monitors, cells, (0, 2)), ("right", EXTERNAL.zone))
        self.assertEqual(resolve_placement(monitors, cells, (0, -1)), ("left", LAPTOP.zone))
        self.assertEqual(resolve_placement(monitors, cells, (-1, 1)), ("top", EXTERNAL.zone))
        self.assertEqual(resolve_placement(monitors, cells, (1, 0)), ("bottom", LAPTOP.zone))

    def test_placement_without_an_adjacent_monitor_uses_the_nearest_one(self):
        monitors = [LAPTOP, EXTERNAL]
        cells = arrange(monitors)
        self.assertEqual(resolve_placement(monitors, cells, (0, 5)), ("right", EXTERNAL.zone))

    def test_inner_edges_between_two_monitors_cannot_hand_over(self):
        monitors = [LAPTOP, EXTERNAL]
        self.assertFalse(is_exterior(monitors, "right", LAPTOP.zone))
        self.assertTrue(is_exterior(monitors, "left", LAPTOP.zone))
        self.assertTrue(is_exterior(monitors, "right", EXTERNAL.zone))
        self.assertTrue(is_exterior(monitors, "top", LAPTOP.zone))

    def test_stacked_monitors_block_the_shared_horizontal_edge(self):
        monitors = [LAPTOP, ABOVE]
        self.assertFalse(is_exterior(monitors, "top", LAPTOP.zone))
        self.assertTrue(is_exterior(monitors, "bottom", LAPTOP.zone))

    def test_one_monitor_draws_the_four_thumbnails_of_the_windows_form(self):
        cells = arrange([LAPTOP])
        shifted, host, rows, columns = normalize(cells, (0, 1), two_row=False)
        # An empty position on each side keeps left and right droppable.
        self.assertEqual(shifted, [(0, 1)])
        self.assertEqual(host, (0, 2))
        self.assertEqual((rows, columns), (1, 4))

    def test_a_host_on_the_left_stays_inside_the_matrix(self):
        cells = arrange([LAPTOP])
        shifted, host, rows, columns = normalize(cells, (0, -1), two_row=False)
        self.assertEqual(shifted, [(0, 2)])
        self.assertEqual(host, (0, 1))
        self.assertEqual((rows, columns), (1, 4))

    def test_two_row_layout_adds_room_above_and_below(self):
        cells = arrange([LAPTOP])
        shifted, host, rows, columns = normalize(cells, (-1, 0), two_row=True)
        self.assertEqual(shifted, [(2, 1)])
        self.assertEqual(host, (1, 1))
        self.assertEqual((rows, columns), (4, 3))

    def test_side_by_side_monitors_keep_both_outer_positions(self):
        monitors = [LAPTOP, EXTERNAL]
        cells = arrange(monitors)
        shifted, host, rows, columns = normalize(cells, (0, 2), two_row=False)
        self.assertEqual(shifted, [(0, 1), (0, 2)])
        self.assertEqual(host, (0, 3))
        self.assertEqual((rows, columns), (1, 5))


if __name__ == "__main__":
    unittest.main()
