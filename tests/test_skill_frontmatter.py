"""Every SKILL.md frontmatter key is one the Agent Skills spec or Claude Code reads.

`quick_validate.py` from skill-creator accepts only the Agent Skills keys. Claude
Code also reads the keys in CLAUDE_CODE_KEYS (subagent fork, model, hooks, ...);
Codex ignores them and still loads the skill. A key in neither list is a typo or
a key nothing reads, so it fails here.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
AGENT_SKILLS_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
CLAUDE_CODE_KEYS = {"when_to_use", "argument-hint", "arguments", "disable-model-invocation", "user-invocable",
                    "disallowed-tools", "model", "effort", "context", "agent", "background", "hooks", "paths",
                    "shell"}


def frontmatter_keys(text):
    """Top-level keys of a SKILL.md's leading `---` block, or None without one."""
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    return re.findall(r"^([A-Za-z_][\w-]*):", match.group(1), re.M)


def unknown_keys(text):
    return [key for key in frontmatter_keys(text) or [] if key not in AGENT_SKILLS_KEYS | CLAUDE_CODE_KEYS]


class FrontmatterTests(unittest.TestCase):
    def test_flags_a_key_nothing_reads(self):
        text = "---\nname: x\ndescription: y\ncontext: fork\nmodle: sonnet\nhooks:\n  Stop: []\n---\n\nbody\n"
        self.assertEqual(frontmatter_keys(text), ["name", "description", "context", "modle", "hooks"])
        self.assertEqual(unknown_keys(text), ["modle"])

    def test_every_skill_uses_known_keys(self):
        skills = sorted(ROOT.glob("*/SKILL.md"))
        self.assertTrue(skills)
        for skill in skills:
            with self.subTest(skill=skill.parent.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIsNotNone(frontmatter_keys(text), "missing frontmatter")
                self.assertEqual(unknown_keys(text), [])


if __name__ == "__main__":
    unittest.main()
