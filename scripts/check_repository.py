"""Check the staged or committed source manifest for runtime data and common credentials."""
import pathlib
import re
import subprocess

def main():
    paths=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    forbidden=[]; secrets=[]
    rules={
        'OpenAI key':r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}',
        'GitHub token':r'\bgh[pousr]_[A-Za-z0-9]{25,}',
        'Google API key':r'\bAIza[A-Za-z0-9_-]{30,}',
        'private key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    }
    for name in paths:
        if not name: continue
        path=pathlib.Path(name)
        if (path.parts[0] in {'data','exports','transcripts','.history','.venv'}
            or path.name in {'.env','token.json','credentials.json'}
            or path.name.startswith('client_secret')
            or path.suffix.lower() in {'.db','.sqlite','.sqlite3','.csv','.xlsx','.xls','.zip','.bin'}):
            forbidden.append(name)
        text=path.read_text(encoding='utf-8',errors='replace')
        for label,pattern in rules.items():
            for match in re.finditer(pattern,text):
                secrets.append((name,text.count('\n',0,match.start())+1,label))
    print(f'Checked {sum(bool(p) for p in paths)} tracked files.')
    for name in forbidden: print('Unexpected runtime/private-data file:',name)
    for name,line,label in secrets: print(f'Possible credential: {name}:{line} ({label})')
    if forbidden or secrets: raise SystemExit(1)
    print('No runtime/customer-data files or common credential patterns found.')

if __name__=='__main__': main()
