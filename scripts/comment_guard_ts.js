const ts = require(process.argv[2]);
const readline = require("readline");

function kindFor(path) {
  if (path.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (path.endsWith(".ts") || path.endsWith(".mts") || path.endsWith(".cts")) return ts.ScriptKind.TS;
  if (path.endsWith(".jsx")) return ts.ScriptKind.JSX;
  return ts.ScriptKind.JS;
}

function comments(path, text) {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, kindFor(path));
  const jsxStarts = new Set();
  const mark = (node) => {
    if (node.kind === ts.SyntaxKind.JsxText || node.kind === ts.SyntaxKind.JsxTextAllWhiteSpaces) {
      jsxStarts.add(node.pos);
    }
    for (const child of node.getChildren(source)) mark(child);
  };
  mark(source);
  const seen = new Map();
  const add = (ranges) => {
    for (const range of ranges || []) {
      if (!seen.has(range.pos)) seen.set(range.pos, range);
    }
  };
  const visit = (node) => {
    if (!jsxStarts.has(node.pos)) add(ts.getLeadingCommentRanges(text, node.pos));
    if (!jsxStarts.has(node.end)) add(ts.getTrailingCommentRanges(text, node.end));
    for (const child of node.getChildren(source)) visit(child);
  };
  visit(source);
  if (!jsxStarts.has(0)) add(ts.getLeadingCommentRanges(text, 0));
  const lines = [];
  for (const range of [...seen.values()].sort((a, b) => a.pos - b.pos)) {
    const start = source.getLineAndCharacterOfPosition(range.pos).line + 1;
    text
      .slice(range.pos, range.end)
      .split("\n")
      .forEach((part, index) => lines.push([start + index, part]));
  }
  return lines;
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  const request = JSON.parse(line);
  let lines = [];
  let failure = null;
  try {
    lines = comments(request.path, request.text);
  } catch (error) {
    failure = String(error);
  }
  process.stdout.write(JSON.stringify({ lines, failure }) + "\n");
});
