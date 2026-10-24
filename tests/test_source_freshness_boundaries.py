"""Publication binds the reviewed per-guide source proof.

Bootstrap approval drift and bounded source reads are tested in test_project_context.
"""
import unittest
from tests import test_guarded_documentation as fixtures


class SourceFreshnessBoundaryTests(unittest.TestCase):
    setUp = fixtures.DocumentationPublicationTests.setUp

    def test_source_drift_after_projection_cannot_publish_fresh_metadata(self):
        (self.root / 'src/main.py').write_text('original = False\n')
        with self.assertRaisesRegex(ValueError, 'source proof'):
            self.docs.finish(self.run, self.receipt, self.projection)
        self.assertEqual((self.root / self.pc.INDEX).read_bytes(), self.original_index)



if __name__ == '__main__':
    unittest.main()
