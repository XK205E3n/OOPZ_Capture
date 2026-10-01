import fs from "node:fs/promises";
import { statSync, readdirSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFileSync } from "node:child_process";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stylesheet = path.join(projectRoot, "tools", "md_to_pdf.css");

export function findChrome(env = process.env, isFile = (file) => {
  try { return statSync(file).isFile(); } catch { return false; }
}) {
  if (env.MD_TO_PDF_CHROME_PATH) {
    if (!isFile(env.MD_TO_PDF_CHROME_PATH)) throw new Error("MD_TO_PDF_CHROME_PATH is not an existing browser executable");
    return env.MD_TO_PDF_CHROME_PATH;
  }
  const home = env.HOME || env.USERPROFILE || os.homedir();
  const cacheRoots = env.PLAYWRIGHT_BROWSERS_PATH
    ? [env.PLAYWRIGHT_BROWSERS_PATH === "0"
      ? path.join(projectRoot, "node_modules", "playwright-core", ".local-browsers")
      : path.resolve(env.PLAYWRIGHT_BROWSERS_PATH)]
    : [path.join(env.XDG_CACHE_HOME || path.join(home, ".cache"), "ms-playwright"),
       path.join(env.LOCALAPPDATA || path.join(home, "AppData", "Local"), "ms-playwright"),
       path.join(home, "Library", "Caches", "ms-playwright")];
  const cached = [];
  for (const root of cacheRoots) {
    let revisions;
    try { revisions = readdirSync(root).filter(name => /^chromium(?:_headless_shell)?-\d+$/.test(name)); }
    catch { continue; }
    revisions.sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
    for (const revision of revisions) {
      for (const executable of ["chrome-linux64/chrome", "chrome-linux/chrome",
          "chrome-win/chrome.exe", "chrome-win64/chrome.exe",
          "chrome-headless-shell-linux64/chrome-headless-shell", "chrome-linux/headless_shell",
          "chrome-headless-shell-win64/chrome-headless-shell.exe"]) {
        cached.push(path.join(root, revision, executable));
      }
    }
  }
  const candidates = [
    ...cached,
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  const browser = candidates.find(isFile);
  if (!browser) throw new Error("PDF browser missing: install Playwright Chromium/Chrome/Edge or set MD_TO_PDF_CHROME_PATH");
  return browser;
}

export function checkChineseFonts(markdown, platform = process.platform, run = execFileSync) {
  if (platform !== "linux" || !/[\u3400-\u9fff\uf900-\ufaff]/u.test(markdown)) return;
  let fonts;
  try { fonts = run("fc-list", [":lang=zh", "family"], { encoding: "utf8", timeout: 10000, stdio: ["ignore", "pipe", "pipe"] }); }
  catch { throw new Error("Chinese PDF font check failed: install fontconfig and fonts-noto-cjk"); }
  if (!fonts.trim()) throw new Error("Chinese PDF fonts missing: install fonts-noto-cjk and rebuild font cache");
}

async function main() {
  const version = process.versions.node.split(".").map(Number);
  if (version[0] < 22 || (version[0] === 22 && version[1] < 12)) {
    throw new Error("PDF dependencies require Node >=22.12.0");
  }
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
  const stage = (name) => process.stderr.write(`PDF_STAGE: ${name}\n`);
  const chrome = findChrome();
  checkChineseFonts(markdown);
  const mdRequire = createRequire(require.resolve("md-to-pdf"));
  const puppeteer = mdRequire("puppeteer");
  const { getHtml } = mdRequire("./lib/get-html.js");
  const { defaultConfig } = mdRequire("./lib/config.js");
  const documentTitle = path.basename(input, path.extname(input)).replace(/[&<>"']/g, "_");
  const html = getHtml(markdown, { ...defaultConfig, document_title: documentTitle });
  let browser;
  try {
    stage("browser-launch");
    browser = await puppeteer.launch({ executablePath: chrome, headless: "new", timeout: 30000, protocolTimeout: 60000 });
    const page = await browser.newPage();
    await page.setJavaScriptEnabled(false);
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      // Reports are text-only. Do not wait for embedded external resources.
      if (request.url().startsWith("data:")) request.continue();
      else request.abort();
    });
    stage("load-content");
    await page.setContent(html, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.addStyleTag({ content: await fs.readFile(stylesheet, "utf8") });
    await page.emulateMediaType("screen");
    stage("print-pdf");
    await page.pdf({
      path: output, timeout: 60000, format: "A4", printBackground: true,
      margin: { top: "18mm", right: "15mm", bottom: "18mm", left: "15mm" },
      displayHeaderFooter: true, headerTemplate: "<span></span>",
      footerTemplate: '<div style="width:100%;font-size:8px;text-align:center;color:#64748b;"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
    });
    stage("complete");
  } finally {
    if (browser) {
      const child = browser.process();
      let timer;
      try {
        await Promise.race([browser.close(), new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("PDF browser close timed out")), 5000);
        })]);
      } finally {
        clearTimeout(timer);
        if (child && child.exitCode === null) child.kill("SIGKILL");
      }
    }
  }
  const outputStat = await fs.stat(output);
  if (!outputStat.isFile() || outputStat.size === 0) {
    throw new Error(`md-to-pdf did not create a non-empty PDF: ${output}`);
  }
  process.stdout.write(JSON.stringify({ input, output, bytes: outputStat.size, result: true }) + "\n");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
