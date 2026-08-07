import unittest

from mwb_linux.topology import coordinates, direction_to, neighbour


class TopologyTests(unittest.TestCase):
    def test_one_row_has_left_and_right_neighbours(self):
        matrix = ["left", "linux", "right", "far"]
        self.assertEqual(coordinates(False), ((0, 0), (0, 1), (0, 2), (0, 3)))
        self.assertEqual(neighbour(matrix, False, "linux", "left"), "left")
        self.assertEqual(neighbour(matrix, False, "linux", "right"), "right")
        self.assertEqual(neighbour(matrix, False, "linux", "top"), "")
        self.assertEqual(neighbour(matrix, False, "right", "right"), "far")

    def test_two_row_has_horizontal_and_vertical_neighbours(self):
        matrix = ["linux", "right", "bottom", "corner"]
        self.assertEqual(coordinates(True), ((0, 0), (0, 1), (1, 0), (1, 1)))
        self.assertEqual(neighbour(matrix, True, "linux", "right"), "right")
        self.assertEqual(neighbour(matrix, True, "linux", "bottom"), "bottom")
        self.assertEqual(direction_to(matrix, True, "right", "corner"), "bottom")

    def test_empty_slot_is_not_a_computer(self):
        self.assertEqual(neighbour(["linux", "", "bottom", ""], True, "linux", "right"), "")

    def test_one_row_skips_empty_slots_and_can_wrap(self):
        matrix = ["left", "", "linux", ""]
        self.assertEqual(neighbour(matrix, False, "left", "right"), "linux")
        self.assertEqual(neighbour(matrix, False, "linux", "right"), "")
        self.assertEqual(
            neighbour(matrix, False, "linux", "right", wrap=True), "left"
        )

    def test_two_row_can_wrap_to_the_opposite_side(self):
        matrix = ["linux", "right", "bottom", "corner"]
        self.assertEqual(
            neighbour(matrix, True, "linux", "left", wrap=True), "right"
        )
        self.assertEqual(
            neighbour(matrix, True, "linux", "top", wrap=True), "bottom"
        )


if __name__ == "__main__":
    unittest.main()
