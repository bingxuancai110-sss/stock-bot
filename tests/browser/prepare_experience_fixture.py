"""Reuse unchanged actual-renderer bodies with the current shared asset links."""
from pathlib import Path
import os,re
source=Path(os.environ['STOCKBOT_UI_BASE'])
target=Path(os.environ['STOCKBOT_UI_ARTIFACTS']);target.mkdir(parents=True,exist_ok=True)
main=(Path(__file__).resolve().parents[2]/'main.py').read_text(encoding='utf-8')
css=re.search(r'/static/app-experience\.css\?v=[^"\s]+',main)[0]
js=re.search(r'/static/app-experience\.js\?v=[^"\s]+',main)[0]
ui_js=re.search(r'/static/ui-feedback\.js\?v=[^"\s]+',main)[0]
for f in source.iterdir():
    if f.suffix not in ('.html','.js'):continue
    text=f.read_text(encoding='utf-8')
    if f.suffix=='.html':
        text=re.sub(r'/static/ui-feedback\.js\?v=[^"\s]+',ui_js,text)
        text=text.replace('</head>',f'<link rel="stylesheet" href="{css}"><script defer src="{js}"></script></head>')
    (target/f.name).write_text(text,encoding='utf-8')
    if f.suffix=='.html' and not f.stem.endswith('-fragment') and '<main id="app-page-content"' in text:
        fragment=target/(f.stem+'-fragment.html')
        if not fragment.exists():
            content=text.split('<main id="app-page-content"',1)[1].split('>',1)[1].split('</main>',1)[0]
            fragment.write_text(content,encoding='utf-8')
print('Unchanged renderer bodies with current shared experience assets:',target)
