# Building docs/Project_Report.docx from docs/PROJECT_REPORT.md

Two passes so the Table of Contents and List of Figures carry real page numbers
(needs Node with the `docx` package, LibreOffice, Poppler and Pillow):

```bash
cd docs
python -c "import re,json;from PIL import Image;md=open('PROJECT_REPORT.md').read();json.dump({p:Image.open(p).size for p in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',md)},open('/tmp/sizes.json','w'))"
node ../scripts/report/md2docx.js . /tmp/pass1.docx /tmp/sizes.json
soffice --headless --convert-to pdf --outdir /tmp /tmp/pass1.docx
python ../scripts/report/pagemap.py PROJECT_REPORT.md /tmp/pass1.pdf /tmp/pagemap.json
node ../scripts/report/md2docx.js . Project_Report.docx /tmp/sizes.json /tmp/pagemap.json
```
