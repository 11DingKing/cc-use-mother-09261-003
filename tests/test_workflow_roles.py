"""角色分权与状态迁移规则的单元测试。"""
import unittest

from service_09261_003.workflow import (
    PUBLISHER,
    REVIEWER,
    SUBMITTER,
    InvalidTransition,
    PermissionDenied,
    resolve,
)


class TestRoleMatrix(unittest.TestCase):
    def test_submitter_actions(self):
        self.assertEqual(resolve("submit", "draft", SUBMITTER), "reviewing")
        self.assertEqual(resolve("revise", "rejected", SUBMITTER), "draft")
        self.assertEqual(resolve("cancel", "draft", SUBMITTER), "cancelled")
        self.assertEqual(resolve("cancel", "rejected", SUBMITTER), "cancelled")

    def test_reviewer_actions(self):
        self.assertEqual(resolve("approve", "reviewing", REVIEWER), "approved")
        self.assertEqual(resolve("reject", "reviewing", REVIEWER), "rejected")

    def test_publisher_actions(self):
        self.assertEqual(resolve("publish", "approved", PUBLISHER), "published")
        self.assertEqual(resolve("archive", "published", PUBLISHER), "archived")

    def test_cross_role_is_forbidden(self):
        with self.assertRaises(PermissionDenied):
            resolve("approve", "reviewing", SUBMITTER)
        with self.assertRaises(PermissionDenied):
            resolve("submit", "draft", REVIEWER)
        with self.assertRaises(PermissionDenied):
            resolve("publish", "approved", REVIEWER)
        with self.assertRaises(PermissionDenied):
            resolve("archive", "published", SUBMITTER)

    def test_action_in_wrong_state(self):
        with self.assertRaises(InvalidTransition):
            resolve("publish", "draft", PUBLISHER)
        with self.assertRaises(InvalidTransition):
            resolve("approve", "approved", REVIEWER)
        with self.assertRaises(InvalidTransition):
            resolve("submit", "published", SUBMITTER)

    def test_unknown_action(self):
        with self.assertRaises(InvalidTransition):
            resolve("delete", "draft", SUBMITTER)

    def test_full_happy_path(self):
        state = "draft"
        for action, role in [
            ("submit", SUBMITTER),
            ("approve", REVIEWER),
            ("publish", PUBLISHER),
            ("archive", PUBLISHER),
        ]:
            state = resolve(action, state, role)
        self.assertEqual(state, "archived")

    def test_reject_and_revise_loop(self):
        state = resolve("submit", "draft", SUBMITTER)
        state = resolve("reject", state, REVIEWER)
        state = resolve("revise", state, SUBMITTER)
        self.assertEqual(state, "draft")


if __name__ == "__main__":
    unittest.main()
