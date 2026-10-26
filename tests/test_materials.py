import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import materials


class MaterialsTest(unittest.TestCase):
    def test_inventory_deduplicates_decoded_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / 'one.png', Path(folder) / 'two.png']
            for index, path in enumerate(paths):
                path.write_bytes(bytes([index]))
            with patch('materials.image_metadata', return_value={'pixel_sha256': 'same', 'width': 2, 'height': 2, 'format': 'PNG'}):
                result = materials.inventory_paths([str(p) for p in paths])
            self.assertEqual(len(result['images']), 2)
            self.assertEqual(len(result['unique_pixels']), 1)
            self.assertNotEqual(result['images'][0]['sha256'], result['images'][1]['sha256'])

    def test_safe_names(self):
        for value in ['../bad', 'CON', 'LPT1.png', 'end ', 'a:b', '.', '']:
            with self.assertRaises(ValueError):
                materials.safe_name(value)
        self.assertEqual(materials.safe_name('图案2'), '图案2')

    def test_copy_resume_and_mismatch_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / 'source.png', Path(folder) / 'output' / 'copy.png'
            source.write_bytes(b'original image bytes placeholder; decoder is not invoked')
            item = {'source': str(source), 'destination': str(destination), 'sha256': materials.file_sha256(source)}
            self.assertEqual(materials.copy_one(item)['status'], 'copied')
            self.assertEqual(materials.copy_one(item)['status'], 'resumed')
            destination.write_bytes(b'user file')
            with self.assertRaises(FileExistsError):
                materials.copy_one(item)
            self.assertEqual(destination.read_bytes(), b'user file')

    def test_changed_source_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.png'
            source.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                materials.copy_one({'source': str(source), 'destination': str(Path(folder) / 'out.png'), 'sha256': 'old'})

    def test_format_extension_and_multiframe_checks(self):
        image = MagicMock()
        image.__enter__.return_value = image
        image.format = 'PNG'
        with patch('PIL.Image.open', return_value=image):
            with self.assertRaises(ValueError):
                materials.image_metadata('wrong.jpg')
            image.n_frames = 2
            with self.assertRaises(ValueError):
                materials.image_metadata('animated.png')

    def test_inventory_invalid_and_unstable_source(self):
        with self.assertRaises(ValueError):
            materials.inventory_paths(['relative.png'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.png'
            path.write_bytes(b'original')
            def changed(_):
                path.write_bytes(b'changed source during inspect')
                return {'pixel_sha256': 'same', 'width': 2, 'height': 2, 'format': 'PNG'}
            with patch('materials.image_metadata', side_effect=changed):
                with self.assertRaises(ValueError):
                    materials.inventory_paths([str(path)])

    def test_exclusive_json_preserves_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'manifest.json'
            materials.exclusive_json(path, {'value': 1})
            with self.assertRaises(FileExistsError):
                materials.exclusive_json(path, {'value': 2})


if __name__ == '__main__':
    unittest.main()
