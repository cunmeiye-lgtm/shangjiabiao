"""Read-only image inventory and byte-preserving, exclusive material copying."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path


def safe_name(value: str) -> str:
    """Validate one exact PSD group/directory name, without normalizing it."""
    if not isinstance(value, str) or not value or value in ('.', '..'):
        raise ValueError(f'Invalid empty/path name: {value!r}')
    reserved = r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)'
    if (re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((' ', '.'))
            or re.match(reserved, value, re.IGNORECASE)):
        raise ValueError(f'Unsafe Windows filename: {value!r}')
    return value


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def image_metadata(path: str | Path) -> dict:
    """Hash decoded, EXIF-oriented RGBA pixels; never save/re-encode the image."""
    from PIL import Image, ImageOps
    accepted = {'PNG': {'.png'}, 'JPEG': {'.jpg', '.jpeg'},
                'WEBP': {'.webp'}, 'TIFF': {'.tif', '.tiff'}, 'BMP': {'.bmp'}}
    with Image.open(path) as image:
        actual = image.format
        if actual not in accepted or Path(path).suffix.lower() not in accepted[actual]:
            raise ValueError(f'Extension does not match supported image format: {path} ({actual})')
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError(f'Multiframe images require explicit preparation: {path}')
        image.load()
        rgba = ImageOps.exif_transpose(image).convert('RGBA')
        digest = hashlib.sha256(f'{rgba.width}x{rgba.height}:RGBA:'.encode())
        digest.update(rgba.tobytes())
        return {'pixel_sha256': digest.hexdigest(), 'width': rgba.width,
                'height': rgba.height, 'format': actual}


def inventory_paths(paths: list[str]) -> dict:
    images, unique = [], {}
    for supplied in sorted(set(paths)):
        path = Path(supplied)
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'Image source must be an accessible absolute file: {path}')
        before = path.stat()
        row = {'source': str(path), 'sha256': file_sha256(path), 'bytes': before.st_size,
               **image_metadata(path)}
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f'Source changed during inventory: {path}')
        images.append(row)
        unique.setdefault(row['pixel_sha256'], []).append(str(path))
    return {'images': images, 'unique_pixels': unique}


def copy_one(item: dict) -> dict:
    """Never overwrite. A matching file resumes; a mismatch blocks execution."""
    source, destination = Path(item['source']), Path(item['destination'])
    if file_sha256(source) != item['sha256']:
        raise ValueError(f'Source hash changed: {source}')
    if destination.exists():
        if not destination.is_file() or file_sha256(destination) != item['sha256']:
            raise FileExistsError(f'Destination differs; preserved for review: {destination}')
        return {'destination': str(destination), 'sha256': item['sha256'], 'status': 'resumed'}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive destination creation prevents clobbering another process/user.
    with source.open('rb') as reader, destination.open('xb') as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    if file_sha256(destination) != item['sha256']:
        raise ValueError(f'Copy hash mismatch; partial output retained for review: {destination}')
    if file_sha256(source) != item['sha256']:
        raise ValueError(f'Source changed while copying: {source}')
    return {'destination': str(destination), 'sha256': item['sha256'], 'status': 'copied'}


def exclusive_json(path: str | Path, data: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
