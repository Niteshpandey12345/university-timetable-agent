"""Find the page of every heading (#, ##) and figure caption in the rendered PDF -> JSON page map."""
import json, re, subprocess, sys

md, pdf, out = sys.argv[1:4]
text = open(md, encoding="utf-8").read()
heads = [(m.group(1), m.group(2).strip()) for m in re.finditer(r"^(#{1,2}) (.+)$", text, re.M)]
figs = re.findall(r"^!\[([^\]]+)\]\(", text, re.M)
n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout).group(1))
norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
pages = [norm(subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), pdf, "-"], capture_output=True, text=True).stdout) for p in range(1, n + 1)]
# body starts at the first page that is not front matter (front pages contain the 999 placeholders or real TOC lines)
start = next(i for i, t in enumerate(pages) if t.startswith("agenticuniversitytimetableallocationsystemabstract") or ("abstract" in t[:120] and "tableofcontents" not in t and "listoffigures" not in t))
pm, cur = {}, start
for _, h in heads:
    key = norm(h)
    for p in range(cur, n):
        if key in pages[p]:
            pm["H:" + h] = p + 1
            cur = p
            break
cur = start
for f in figs:
    key = norm(f)[:60]
    for p in range(cur, n):
        if key in pages[p]:
            pm["F:" + f] = p + 1
            cur = p
            break
missing = [h for _, h in heads if "H:" + h not in pm] + [f for f in figs if "F:" + f not in pm]
json.dump(pm, open(out, "w"), indent=1)
print(f"pages={n} body_start={start + 1} mapped={len(pm)} missing={missing}")
