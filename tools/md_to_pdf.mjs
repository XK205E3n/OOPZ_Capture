import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import mdToPdfModule from "md-to-pdf";

const { mdToPdf } = mdToPdfModule;
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stylesheet = path.join(projectRoot, "tools", "md_to_pdf.css");

function findChrome() {
  const candidates = [
    process.env.MD_TO_PDF_CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  return candidates[0];
}

async function main() {
  const input = path.resolve(process.argv[2] || "");
  const output = path.resolve(process.argv[3] || "");
  if (!input || !output || input === projectRoot || output === projectRoot) {
    throw new Error("usage: node tools/md_to_pdf.mjs <input.md> <output.pdf>");
  }
  const inputStat = await fs.stat(input);
  if (!inputStat.isFile() || path.extname(input).toLowerCase() !== ".md") {
    throw new Error(`Markdown input does not exist or is not .md: ${input}`);
  }
  await fs.mkdir(path.dirname(output), { recursive: true });
  const markdown = await fs.readFile(input, "utf8");
  const launchOptions = {};
  const chrome = findChrome();
  if (chrome) launchOptions.executablePath = chrome;
  launchOptions.headless = "new";
  const result = await mdToPdf(
    // Pass trusted Markdown as content so md-to-pdf's preliminary navigation
    // always targets a real index page. This avoids a Windows server edge case
    // where <file>.md/index.html remains pending.
    { content: markdown },
    {
      dest: output,
      // Keep the served directory scoped to the Markdown file's directory.
      // md-to-pdf requests <relative-path>/index.html before replacing the page
      // content; using the project root would make a regular .md file look like
      // a directory on Windows and can leave that request pending.
      basedir: projectRoot,
      document_title: path.basename(input, path.extname(input)),
      stylesheet: [stylesheet],
      launch_options: launchOptions,
      pdf_options: {
        format: "A4",
        printBackground: true,
        margin: { top: "18mm", right: "15mm", bottom: "18mm", left: "15mm" },
        displayHeaderFooter: true,
        headerTemplate: "<span></span>",
        footerTemplate: '<div style="width:100%;font-size:8px;text-align:center;color:#64748b;"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
      },
    },
  );
  const outputStat = await fs.stat(output);
  if (!outputStat.isFile() || outputStat.size === 0) {
    throw new Error(`md-to-pdf did not create a non-empty PDF: ${output}`);
  }
  process.stdout.write(JSON.stringify({ input, output, bytes: outputStat.size, result: result ? true : false }) + "\n");
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
