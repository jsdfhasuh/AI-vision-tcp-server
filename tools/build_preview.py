"""Rebuild the self-contained, clearly labelled preview after changing assets."""
from pathlib import Path


def main() -> None:
    root=Path(__file__).resolve().parents[1]
    html=(root/'competition/web/index.html').read_text(encoding='utf-8')
    html=html.replace('content="live"','content="demo"')
    html=html.replace('<title>工业视觉技能竞赛 · 现场展板</title>','<title>界面演示 · 非比赛数据</title>')
    html=html.replace('<link rel="icon" href="/favicon.svg" type="image/svg+xml">','')
    html=html.replace('<link rel="stylesheet" href="/style.css">','<style>\n'+(root/'competition/web/style.css').read_text(encoding='utf-8')+'\n</style>')
    html=html.replace('<script src="/app.js" defer></script>','')
    html=html.replace('</body>','<script>\n'+(root/'competition/web/app.js').read_text(encoding='utf-8')+'\n</script>\n</body>')
    (root/'preview_display.html').write_text(html,encoding='utf-8')

if __name__=='__main__':main()
