// Converts docs/PROJECT_REPORT.md (a known Markdown subset) into a styled Word report.
// usage: node md2docx.js <docsDir> <out.docx> <imgsizes.json>
const fs = require("fs"), path = require("path");
const D = require("docx");
const { Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell, WidthType, Bookmark, InternalHyperlink, PositionalTab, PositionalTabAlignment, PositionalTabLeader, PositionalTabRelativeTo,
  ShadingType, BorderStyle, ImageRun, PageBreak, TableOfContents, Footer, Header, PageNumber, LevelFormat, TabStopType } = D;

const [docsDir, outFile, sizesFile, pageMapFile] = process.argv.slice(2);
const pageMap = pageMapFile && fs.existsSync(pageMapFile) ? JSON.parse(fs.readFileSync(pageMapFile, "utf8")) : {};
const sizes = JSON.parse(fs.readFileSync(sizesFile, "utf8"));
let md = fs.readFileSync(path.join(docsDir, "PROJECT_REPORT.md"), "utf8");

// ---- front matter
const fm = {};
md = md.replace(/^---\n([\s\S]*?)\n---\n/, (_, b) => { b.split("\n").forEach(l => { const i = l.indexOf(":"); fm[l.slice(0, i).trim()] = l.slice(i + 1).trim(); }); return ""; });

const FONT = "Times New Roman", MONO = "Courier New", ACCENT = "1F3A5F", LIGHT = "E8EEF7";
const CONTENT_W = 9026; // A4 with 1" margins, DXA

function inline(text, base = {}) {
  const runs = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) runs.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const t = m[0];
    if (t.startsWith("**")) runs.push(new TextRun({ text: t.slice(2, -2), bold: true, ...base }));
    else if (t.startsWith("`")) runs.push(new TextRun({ text: t.slice(1, -1), font: MONO, size: (base.size || 24) - 4, color: "8A2B5A", ...base, bold: base.bold }));
    else runs.push(new TextRun({ text: t.slice(1, -1), italics: true, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) runs.push(new TextRun({ text: text.slice(last), ...base }));
  return runs.map(r => r);
}
const clean = s => s.replace(/\\\*/g, "*");

function table(rows) {
  const ncol = rows[0].length;
  const strip = t => (t || "").replace(/[*`\\]/g, "");
  const avg = Array.from({ length: ncol }, (_, c) => rows.reduce((a, r) => a + strip(r[c]).length, 0) / rows.length);
  const word = Array.from({ length: ncol }, (_, c) => Math.max(...rows.map(r => Math.max(0, ...strip(r[c]).split(/[\s,]+/).map(w => w.length)))));
  // a column must fit its longest unbreakable word (code-ish words are wider); the rest share the remainder
  const isCode = Array.from({ length: ncol }, (_, c) => rows.slice(1).some(r => /`/.test(r[c] || "")));
  let minW = word.map((w, c) => w * (isCode[c] ? 122 : 100) + 280);
  const sumMin = minW.reduce((a, b) => a + b, 0);
  if (sumMin > CONTENT_W) minW = minW.map(m => m * CONTENT_W / sumMin);
  const extra = CONTENT_W - minW.reduce((a, b) => a + b, 0);
  const want = avg.map((a, c) => Math.max(0, a * 100 - minW[c]) + 1);
  const wantTot = want.reduce((a, b) => a + b, 0);
  let widths = minW.map((m, c) => m + extra * want[c] / wantTot);
  widths = widths.map(w => Math.floor(w));
  widths[widths.length - 1] += CONTENT_W - widths.reduce((a, b) => a + b, 0);
  const border = { style: BorderStyle.SINGLE, size: 4, color: "BFC8D6" };
  const borders = { top: border, bottom: border, left: border, right: border };
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths,
    rows: rows.map((r, ri) => new TableRow({
      tableHeader: ri === 0, cantSplit: true,
      children: r.map((cell, ci) => new TableCell({
        width: { size: widths[ci], type: WidthType.DXA }, borders,
        shading: ri === 0 ? { type: ShadingType.CLEAR, color: "auto", fill: ACCENT } : (ri % 2 === 0 ? { type: ShadingType.CLEAR, color: "auto", fill: "F5F7FB" } : undefined),
        margins: { top: 50, bottom: 50, left: 90, right: 90 },
        children: [new Paragraph({ spacing: { before: 0, after: 0 }, children: inline(clean(cell.trim()), ri === 0 ? { bold: true, color: "FFFFFF", size: 20 } : { size: 20 }) })],
      })),
    })),
  });
}

function image(rel, caption) {
  const file = path.join(docsDir, rel);
  const [w, h] = sizes[rel];
  const maxW = 6.2 * 96, maxH = 8.0 * 96; // px @96dpi
  let tw = maxW, th = h * maxW / w;
  if (th > maxH) { th = maxH; tw = w * maxH / h; }
  if (rel.includes("graph.png")) { const s = Math.min(1, (5.4 * 96) / tw); tw *= s; th *= s; }
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 160, after: 60 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(file), transformation: { width: Math.round(tw), height: Math.round(th) },
        altText: { title: caption, description: caption, name: path.basename(rel) } })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: inline(caption, { italics: true, size: 21, color: "333333" }) }),
  ];
}

const body = [];
const lines = md.split("\n");
let i = 0, para = [], numInst = 0, lastNum = false, pendingBreak = false, hid = 0;
const headings = [], figures = [];
const flush = () => { if (para.length) { body.push(new Paragraph({ spacing: { after: 140, line: 288 }, alignment: AlignmentType.JUSTIFIED, children: inline(clean(para.join(" "))) })); para = []; } };
while (i < lines.length) {
  const l = lines[i];
  if (l.trim() === "<!-- pagebreak -->") { flush(); pendingBreak = true; i++; continue; }
  if (l.startsWith("```")) {
    flush(); i++; const code = [];
    while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
    i++;
    code.forEach((c, k) => body.push(new Paragraph({ spacing: { before: k ? 0 : 80, after: k === code.length - 1 ? 160 : 0, line: 240 },
      shading: { type: ShadingType.CLEAR, color: "auto", fill: "F3F4F6" }, children: [new TextRun({ text: c || " ", font: MONO, size: 16 })] })));
    continue;
  }
  let m;
  if ((m = l.match(/^(#{1,3}) (.*)/))) {
    flush(); lastNum = false;
    const lvl = m[1].length, txt = m[2], id = "h" + (++hid);
    headings.push({ lvl, txt, id });
    body.push(new Paragraph({ heading: [HeadingLevel.HEADING_1, HeadingLevel.HEADING_2, HeadingLevel.HEADING_3][lvl - 1],
      pageBreakBefore: lvl === 1 && (headings.length === 1 || (pendingBreak && /^(1\. Introduction|References|Appendix A)/.test(txt))),
      children: [new Bookmark({ id, children: [new TextRun(txt)] })] }));
    pendingBreak = false;
    i++; continue;
  }
  if ((m = l.match(/^!\[([^\]]*)\]\(([^)]+)\)/))) { flush(); figures.push(m[1]); body.push(...image(m[2], m[1])); i++; continue; }
  if (l.startsWith("|")) {
    flush(); lastNum = false; const rows = [];
    while (i < lines.length && lines[i].startsWith("|")) {
      if (!/^\|[\s\-:|]+\|\s*$/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map(s => s.replace(/\\\|/g, "|")));
      i++;
    }
    body.push(table(rows), new Paragraph({ spacing: { after: 120 }, children: [] }));
    continue;
  }
  if ((m = l.match(/^\s*[*-] (.*)/))) {
    flush(); lastNum = false; let t = m[1]; i++;
    while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*[*-] /.test(lines[i])) t += " " + lines[i++].trim();
    body.push(new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 60, line: 276 }, children: inline(clean(t)) }));
    continue;
  }
  if ((m = l.match(/^(\d+)\. (.*)/))) {
    flush(); if (!lastNum) numInst++; lastNum = true; let t = m[2]; i++;
    while (i < lines.length && /^\s{2,}\S/.test(lines[i])) t += " " + lines[i++].trim();
    body.push(new Paragraph({ numbering: { reference: "numbers", level: 0, instance: numInst }, spacing: { after: 60, line: 276 }, children: inline(clean(t)) }));
    continue;
  }
  if ((m = l.match(/^\[(\d+)\] (.*)/))) {
    flush(); body.push(new Paragraph({ spacing: { after: 100 }, indent: { left: 567, hanging: 567 }, children: [new TextRun({ text: `[${m[1]}]\t`, bold: true }), ...inline(m[2])], tabStops: [{ type: TabStopType.LEFT, position: 567 }] }));
    i++; continue;
  }
  if (l.trim() === "") { flush(); i++; continue; }
  lastNum = false;
  para.push(l.trim()); i++;
}
flush();

// ---- static table of contents / list of figures (page numbers from a first rendering pass)
function tocLine(text, page, indent, bold, anchor) {
  const label = anchor ? new InternalHyperlink({ anchor, children: [new TextRun({ text, bold })] }) : new TextRun({ text, bold });
  return new Paragraph({ spacing: { before: bold ? 100 : 20, after: 20 }, indent: { left: indent },
    tabStops: [{ type: TabStopType.RIGHT, position: CONTENT_W, leader: "dot" }],
    children: [label, new TextRun({ text: "\t" + String(page ?? "999"), bold })] });
}
function tocBlock() {
  const out = [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 240 }, children: [new TextRun({ text: "TABLE OF CONTENTS", bold: true, size: 32, color: ACCENT })] })];
  for (const h of headings) if (h.lvl <= 2) out.push(tocLine(h.txt, pageMap["H:" + h.txt], h.lvl === 1 ? 0 : 400, h.lvl === 1, h.id));
  out.push(new Paragraph({ children: [new PageBreak()] }));
  out.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 240 }, children: [new TextRun({ text: "LIST OF FIGURES", bold: true, size: 32, color: ACCENT })] }));
  for (const f of figures) { const t = f.replace(/\.$/, ""); out.push(tocLine(t, pageMap["F:" + f], 0, false)); }
  return out;
}

// ---- title page
const title = [
  new Paragraph({ spacing: { before: 1800 }, alignment: AlignmentType.CENTER, children: [new TextRun({ text: "PROJECT REPORT", bold: true, size: 26, color: "6B7A90", characterSpacing: 60 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 300, after: 200 }, children: [new TextRun({ text: fm.title, bold: true, size: 52, color: ACCENT })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 }, children: [new TextRun({ text: fm.subtitle, size: 26, color: "374151" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, border: { top: { style: BorderStyle.SINGLE, size: 8, color: ACCENT, space: 12 } }, spacing: { before: 200, after: 120 }, children: [new TextRun({ text: "Submitted by", size: 22, color: "6B7A90" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 }, children: [new TextRun({ text: fm.author, bold: true, size: 30 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 }, children: [new TextRun({ text: fm.course, size: 22 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 }, children: [new TextRun({ text: fm.date, size: 22 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Technologies: LangGraph · Model Context Protocol · OR-Tools CP-SAT · FastAPI · Python 3.14", size: 20, color: "4A5568" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120 }, children: [new TextRun({ text: "Code: " + fm.repo, size: 20, color: "2B6CB0" })] }),
  new Paragraph({ children: [new PageBreak()] }),
  ...tocBlock(),
];

const doc = new Document({
  creator: fm.author, title: fm.title, description: fm.subtitle,
  features: { updateFields: true },
  styles: {
    default: { document: { run: { font: FONT, size: 24 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 34, bold: true, color: ACCENT }, paragraph: { spacing: { before: 240, after: 160 }, keepNext: true, outlineLevel: 0,
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "C7D2E3", space: 4 } } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 27, bold: true, color: "2C5282" }, paragraph: { spacing: { before: 240, after: 100 }, keepNext: true, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 23, bold: true, color: "2D3748" }, paragraph: { spacing: { before: 180, after: 80 }, keepNext: true, outlineLevel: 2 } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] },
    { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
  ] },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1440, bottom: 1300, left: 1440, right: 1440 } }, titlePage: true },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: "Agentic University Timetable Allocation System", size: 17, color: "8A94A6" })] })] }) },
    footers: {
      default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Page ", size: 18, color: "8A94A6" }), new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "8A94A6" })] })] }),
      first: new Footer({ children: [new Paragraph({ children: [] })] }),
    },
    children: [...title, ...body],
  }],
});
Packer.toBuffer(doc).then(b => { fs.writeFileSync(outFile, b); console.log("wrote", outFile, b.length); });
