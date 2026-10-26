import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from template_cache import check_documents


class CacheTests(unittest.TestCase):
    def test_metadata_strict_changed_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'metadata-fixture.txt'
            path.write_bytes(b'existing template version')
            stat=path.stat()
            item={'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                  'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertEqual(check_documents([item])[0]['status'],'metadata_match')
            self.assertEqual(check_documents([item],True)[0]['status'],'hash_match')
            self.assertEqual(check_documents([{**item,'mtime_ns':0}])[0]['status'],'hash_match')
            path.write_bytes(b'new document version')
            self.assertTrue(check_documents([item])[0]['reinspect'])
            self.assertEqual(check_documents([{**item,'path':str(path.with_name('missing'))}])[0]['status'],'unavailable')


if __name__=='__main__':unittest.main()
