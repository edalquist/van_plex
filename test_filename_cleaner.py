# test_filename_cleaner.py

import unittest
from filename_cleaner import clean_filename

class TestFilenameCleaner(unittest.TestCase):

    def test_with_tags_and_season_episode(self):
        """
        Tests a typical TV show name with season/episode and multiple tags.
        """
        original = "Galactic Adventures (3022) - S01E05 - The Nebula's Edge [UHD-4K][DTS-HD MA][x265]-TEAM"
        expected = "Galactic Adventures (3022) - S01E05 - The Nebula's Edge"
        self.assertEqual(clean_filename(original), expected)

    def test_with_multiple_tags(self):
        """
        Tests a movie title with multiple bracketed tags.
        """
        original = "The Legend of Eldoria (1998) [game-id-4567][BR-720p][AC3 2.0][x264]"
        expected = "The Legend of Eldoria (1998)"
        self.assertEqual(clean_filename(original), expected)

    def test_with_junk_after_tags(self):
        """
        Tests a filename with non-bracketed junk after the tags.
        The logic should cut at the first bracket, so the junk is also removed.
        """
        original = "Quantum Quest (2042) Director's Cut [scifi-001]-RELEASEGRP"
        expected = "Quantum Quest (2042) Director's Cut"
        self.assertEqual(clean_filename(original), expected)

    def test_no_tags(self):
        """
        Tests a filename that has no tags to remove.
        """
        original = "Fantastic Voyage (2025)"
        expected = "Fantastic Voyage (2025)"
        self.assertEqual(clean_filename(original), expected)

    def test_no_year_or_tags(self):
        """
        Tests a simple filename with no year or tags.
        """
        original = "The Great Escape Quest"
        expected = "The Great Escape Quest"
        self.assertEqual(clean_filename(original), expected)

    def test_empty_string(self):
        """
        Tests an empty string to ensure it doesn't crash.
        """
        original = ""
        expected = ""
        self.assertEqual(clean_filename(original), expected)

    def test_only_tags(self):
        """
        Tests a string that consists only of tags.
        """
        original = "[WEBDL-1080p][h264]"
        expected = ""
        self.assertEqual(clean_filename(original), expected)
        
    def test_filename_with_leading_whitespace(self):
        """
        Tests that leading whitespace is preserved if it's before the first tag.
        """
        original = "  Space Opera Film (2077) [vfx-showcase]"
        expected = "  Space Opera Film (2077)"
        self.assertEqual(clean_filename(original), expected)

    def test_filename_with_trailing_whitespace_before_tag(self):
        """
        Tests that trailing whitespace before a tag is correctly stripped.
        """
        original = "Adventure Begins (2030)   [extended-cut]"
        expected = "Adventure Begins (2030)"
        self.assertEqual(clean_filename(original), expected)

if __name__ == '__main__':
    unittest.main()
