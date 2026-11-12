"""3.0.1 regression: HU/DE answers had English section headings (vision + smart diagnose)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_core  # noqa: E402


class PromptLanguageTest(unittest.TestCase):
    def test_vision_headings_are_given_in_the_answer_language(self):
        for lang, must, must_not in (("hu", "Látható jelek", "Visible evidence"), ("de", "Sichtbare Hinweise", "Visible evidence"), ("en", "Visible evidence", "Látható jelek")):
            p = ai_core.vision_prompt({"language": lang, "mode": "display"})
            self.assertIn(must, p)
            self.assertNotIn(must_not, p.split("CODES:")[0])
            self.assertIn("CODES:", p)                      # machine-readable last line stays English (parsed by the apps)

    def test_smart_headings_and_status_words_are_localized(self):
        hu = ai_core.smart_prompt("hu", "case data long enough")
        self.assertIn("Legvalószínűbb ok most", hu); self.assertIn("BIZONYÍTOTT", hu); self.assertNotIn("1. Most likely cause", hu)
        de = ai_core.smart_prompt("de", "case data long enough")
        self.assertIn("Nächste sichere Messung", de); self.assertIn("VERMUTET", de)
        en = ai_core.smart_prompt("en", "case data long enough")
        self.assertIn("Most likely cause now", en)


if __name__ == "__main__":
    unittest.main()
