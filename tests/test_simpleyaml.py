import unittest
from rip_swarm.simpleyaml import load_yaml

class TestSimpleYaml(unittest.TestCase):
    def test_nested_and_lists(self):
        text = (
            "name: default\n"
            "flag: true\n"
            "missing: null\n"
            "count: 2\n"
            "budget:\n"
            "  max_claims_open_per_agent: 1\n"
            "slash:\n"
            "  enabled:\n"
            "    - lookback\n"
            "    - status\n"
        )
        doc = load_yaml(text)
        self.assertEqual(doc["name"], "default")
        self.assertIs(doc["flag"], True)
        self.assertIsNone(doc["missing"])
        self.assertEqual(doc["count"], 2)
        self.assertEqual(doc["budget"]["max_claims_open_per_agent"], 1)
        self.assertEqual(doc["slash"]["enabled"], ["lookback", "status"])

    def test_list_of_maps(self):
        doc = load_yaml("- id: alice\n  harness: claude-code\n  role: worker\n")
        self.assertEqual(doc, [{"id": "alice", "harness": "claude-code", "role": "worker"}])

    def test_flow_lists_and_path_scalar(self):
        doc = load_yaml("operators: []\nslash:\n  enabled: [lookback, status]\nlookback:\n  write_dir: lookback/\n")
        self.assertEqual(doc["operators"], [])
        self.assertEqual(doc["slash"]["enabled"], ["lookback", "status"])
        self.assertEqual(doc["lookback"]["write_dir"], "lookback/")
        self.assertEqual(load_yaml("[]\n"), [])

if __name__ == "__main__":
    unittest.main()
