"""Read-only version check for already-inspected PSD files; never parses or edits PSD."""
import argparse
import hashlib
import json
from pathlib import Path


def check_documents(documents, strict=False):
    results = []
    for item in documents:
        path = Path(item['path'])
        try:
            stat = path.stat()
            same = stat.st_size == item['bytes'] and stat.st_mtime_ns == item['mtime_ns']
            if same and not strict:
                results.append({'path':str(path),'status':'metadata_match','reinspect':False})
                continue
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream,'sha256').hexdigest()
            unchanged = digest == item['sha256']
            results.append({'path':str(path),'status':'hash_match' if unchanged else 'changed',
                            'reinspect':not unchanged,'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                            'sha256':digest})
        except OSError as exc:
            results.append({'path':str(path),'status':'unavailable','reinspect':True,'error':str(exc)})
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile');parser.add_argument('--strict',action='store_true')
    args=parser.parse_args()
    profile=json.loads(Path(args.profile).read_text('utf-8'))
    documents=profile.get('documents')
    if documents is None:
        documents=[doc for template in profile['templates'] for doc in template['documents']]
    if not documents:raise ValueError('No cached PSD documents')
    result=check_documents(documents,args.strict)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(any(x['reinspect'] for x in result))


if __name__=='__main__':raise SystemExit(main())
