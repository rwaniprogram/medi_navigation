# 지식베이스(kb_gen.py) + 엔진(engine.js) + 화면(template.html) 을 합쳐 Newdiagnosis.html 과 공유용 share/medinavi-share.html 을 만든다.
# 사용법: python tools/build.py
import subprocess, sys, os
d=os.path.dirname(os.path.abspath(__file__))
kb=subprocess.check_output([sys.executable, os.path.join(d,'kb_gen.py')]).decode()
eng=open(os.path.join(d,'engine.js')).read()
t=open(os.path.join(d,'template.html')).read()
assert '</script' not in kb
out=t.replace('__KB_JSON__',kb).replace('__ENGINE_JS__',eng)
open(os.path.join(d, '..', 'Newdiagnosis.html'),'w').write(out)
print(len(out))
# Shareable static variant (no server; artifact host wraps the skeleton)
import re as _re
share = out
share = share.replace('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="color-scheme" content="light dark">\n', '')

share = share.replace('</style>\n</head>\n<body>', '</style>\n<script>window.MEDINAVI_STATIC = true;</script>')
share = share.replace('</body>\n</html>', '')
assert '<head>' not in share and '<body>' not in share and 'MEDINAVI_STATIC = true' in share
os.makedirs(os.path.join(d, '..', 'share'), exist_ok=True)
open(os.path.join(d, '..', 'share', 'medinavi-share.html'),'w').write(share)
