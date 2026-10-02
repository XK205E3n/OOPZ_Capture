// Shared Markdown parser for the explicit browser-free PDF backend.
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { buildReportHtml, checkChineseFonts } from "./md_to_pdf.mjs";

async function main() {
  const input = path.resolve(process.argv[2] || "");
  const maximum = 10 * 1024 * 1024;
  const handle = await fs.open(input, "r");
  let markdown;
  try {
    const info = await handle.stat();
    if (!info.isFile() || path.extname(input).toLowerCase() !== ".md") throw new Error("Expected a Markdown file");
    if (info.size > maximum) throw new Error("Markdown report exceeds the 10 MiB render limit");
    const buffer = Buffer.alloc(maximum + 1);
    let length = 0;
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, length);
      if (!bytesRead) break;
      length += bytesRead;
    }
    if (length > maximum) throw new Error("Markdown report exceeds the 10 MiB render limit");
    markdown = buffer.subarray(0, length).toString("utf8");
  } finally {
    await handle.close();
  }
  checkChineseFonts(markdown);
  const html = buildReportHtml(markdown, path.basename(input, path.extname(input)));
  if (Buffer.byteLength(html, "utf8") > 32 * 1024 * 1024) throw new Error("Expanded HTML exceeds the 32 MiB render limit");
  process.stdout.write(html);
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
