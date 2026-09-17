import unittest
from rip_swarm.simpleyaml import load_yaml

# Copied VERBATIM from docs/specs/2026-09-17-design-spec.md section 9 (the
# ```yaml block, inline comments and all). If the spec block changes, this
# string must be re-copied.
SPEC_PROFILE = """name: default
reviews_required_per_plan: 1   # advisory for the orchestrator; helpers do not enforce
orchestrator_lease_ttl: 30m
worker_lease_ttl: 15m
allow_self_promote: false
allow_preempt: false           # reserved; ignored by v0 helpers
operators: []                  # agent ids allowed to promote anyone (incl. themselves)
budget:
  max_claims_open_per_agent: 1
  spend_requires_operator: true
lookback:
  min_messages_before_run: 20
  write_dir: lookback/         # relative to hive root
slash:
  enabled: [lookback, status]  # advisory for harness integrations; helpers ignore
"""


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

    # --- comments (item 1) ---

    def test_full_line_comment_is_skipped(self):
        doc = load_yaml("# header comment\nname: default\n# another\nallow_preempt: false\n")
        self.assertEqual(doc["name"], "default")
        self.assertIs(doc["allow_preempt"], False)

    def test_indented_full_line_comment_is_skipped(self):
        doc = load_yaml("budget:\n  # inner note\n  max_claims_open_per_agent: 1\n")
        self.assertEqual(doc["budget"]["max_claims_open_per_agent"], 1)

    def test_inline_comment_stripped_from_bool(self):
        doc = load_yaml("allow_self_promote: false  # note\n")
        self.assertIs(doc["allow_self_promote"], False)

    def test_inline_comment_stripped_from_int(self):
        doc = load_yaml("max_claims_open_per_agent: 1  # x\n")
        self.assertEqual(doc["max_claims_open_per_agent"], 1)

    def test_inline_comment_stripped_from_flow_list(self):
        doc = load_yaml("operators: []                  # agent ids\n")
        self.assertEqual(doc["operators"], [])
        doc = load_yaml("enabled: [lookback, status]  # advisory\n")
        self.assertEqual(doc["enabled"], ["lookback", "status"])

    def test_inline_comment_stripped_in_list_items(self):
        doc = load_yaml("- alice  # first\n- bob # second\n")
        self.assertEqual(doc, ["alice", "bob"])

    def test_hash_without_leading_space_is_literal(self):
        doc = load_yaml("colour: '#ff0000'\nfrag: a#b\n")
        self.assertEqual(doc["colour"], "#ff0000")
        self.assertEqual(doc["frag"], "a#b")

    def test_hash_inside_quotes_is_not_a_comment(self):
        doc = load_yaml('note: "a # b"  # real comment\n')
        self.assertEqual(doc["note"], "a # b")
        doc = load_yaml("note: 'a # b'\n")
        self.assertEqual(doc["note"], "a # b")

    def test_spec_section_9_profile_block_verbatim(self):
        doc = load_yaml(SPEC_PROFILE)
        self.assertEqual(doc["name"], "default")
        self.assertEqual(doc["reviews_required_per_plan"], 1)
        self.assertEqual(doc["orchestrator_lease_ttl"], "30m")
        self.assertEqual(doc["worker_lease_ttl"], "15m")
        self.assertIs(doc["allow_self_promote"], False)
        self.assertIs(doc["allow_preempt"], False)
        self.assertEqual(doc["operators"], [])
        self.assertEqual(doc["budget"]["max_claims_open_per_agent"], 1)
        self.assertIs(doc["budget"]["spend_requires_operator"], True)
        self.assertEqual(doc["lookback"]["min_messages_before_run"], 20)
        self.assertEqual(doc["lookback"]["write_dir"], "lookback/")
        self.assertEqual(doc["slash"]["enabled"], ["lookback", "status"])

    # --- quoted scalars (item 2) ---

    def test_double_quoted_scalar_strips_quotes(self):
        doc = load_yaml('id: "*"\nname: "alice"\n')
        self.assertEqual(doc["id"], "*")
        self.assertEqual(doc["name"], "alice")

    def test_single_quoted_scalar_strips_quotes(self):
        doc = load_yaml("id: '*'\nempty: ''\n")
        self.assertEqual(doc["id"], "*")
        self.assertEqual(doc["empty"], "")

    def test_quoted_scalar_is_not_coerced(self):
        doc = load_yaml('a: "true"\nb: "false"\nc: "null"\nd: "7"\n')
        self.assertEqual(doc["a"], "true")
        self.assertEqual(doc["b"], "false")
        self.assertEqual(doc["c"], "null")
        self.assertEqual(doc["d"], "7")
        for key in ("a", "b", "c", "d"):
            self.assertIsInstance(doc[key], str)

    def test_mismatched_quotes_kept_verbatim(self):
        doc = load_yaml("a: \"unclosed\nb: 'x\"\n")
        self.assertEqual(doc["a"], '"unclosed')
        self.assertEqual(doc["b"], "'x\"")

    def test_quoted_scalars_in_flow_list(self):
        doc = load_yaml('ids: ["*", "orchestrator"]\n')
        self.assertEqual(doc["ids"], ["*", "orchestrator"])

    def test_quoted_list_item(self):
        doc = load_yaml('- "*"\n- plain\n')
        self.assertEqual(doc, ["*", "plain"])

    # --- tabs (item 3) ---

    def test_tab_in_leading_whitespace_raises(self):
        with self.assertRaises(ValueError):
            load_yaml("budget:\n\tmax_claims_open_per_agent: 1\n")

    def test_tab_mixed_with_spaces_in_indent_raises(self):
        with self.assertRaises(ValueError):
            load_yaml("budget:\n  \tmax_claims_open_per_agent: 1\n")

    def test_tab_after_content_is_allowed(self):
        doc = load_yaml("name:\tdefault\n")
        self.assertEqual(doc["name"], "default")


if __name__ == "__main__":
    unittest.main()
