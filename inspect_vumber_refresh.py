from pathlib import Path
import re
for file in Path('data/wild_horse/vendor').glob('*.js'):
    text=file.read_text(encoding='utf-8')
    for match in re.finditer(r'grantType:"refresh_token"|grantType:"refreshToken"|refreshToken\(',text):
        print(file.name,text[max(0,match.start()-350):match.start()+1100])
