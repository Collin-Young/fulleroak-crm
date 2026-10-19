import requests,re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
root=Path('data/wild_horse/vendor'); root.mkdir(exist_ok=True)
html=requests.get('https://app.vumber.com',timeout=25).text
scripts=re.findall(r'<script[^>]+src=["\x27]([^"\x27]+)',html)
scripts=[s for s in scripts if not s.startswith('http')]
print(scripts)
for path in scripts:
    if path.startswith('http'): url=path
    else: url='https://app.vumber.com/'+path.lstrip('/')
    data=requests.get(url,timeout=25).text
    (root/Path(path).name).write_text(data,encoding='utf-8')
    for match in re.finditer(r'uploadIds|uploadFile|/upload|multipart',data):
        print(Path(path).name, data[max(0,match.start()-250):match.end()+300])
text=(root/'chunk-MZQATPSH.js').read_text(encoding='utf-8') if (root/'chunk-MZQATPSH.js').exists() else ''
for word in ['accept','maxFileSize','file[]','name","file']:
    for match in re.finditer(re.escape(word),text): print(text[max(0,match.start()-180):match.start()+250])
main=(root/'main-VI2BVMFI.js').read_text(encoding='utf-8')
chunks=set(re.findall(r'chunk-[A-Z0-9]+\.js',main))
def fetch(name):
    data=requests.get('https://app.vumber.com/'+name,timeout=25).text
    (root/name).write_text(data,encoding='utf-8')
    found=[]
    for match in re.finditer(r'uploadIds|uploadFile|/upload|multipart',data):
        found.append((name,data[max(0,match.start()-350):match.end()+500]))
    return found
with ThreadPoolExecutor(max_workers=6) as pool:
    for result in pool.map(fetch,chunks):
        for name,text in result: print(name,text)
