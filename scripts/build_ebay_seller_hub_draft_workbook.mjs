import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const projectDir = "/Users/seanfouz/Documents/Horizon Ai Agents";
const outputDir = path.join(projectDir, "outputs");
const csvPath = path.join(
  outputDir,
  "ebay-seller-hub-visible-drafts-2026-07-24.csv",
);
const xlsxPath = path.join(
  outputDir,
  "ebay-seller-hub-visible-drafts-2026-07-24.xlsx",
);
const previewPath = path.join(
  outputDir,
  "ebay-seller-hub-visible-drafts-preview.png",
);

const auditPaths = [
  "/private/tmp/ebay_image_audit_0.json",
  "/private/tmp/ebay_image_audit_25.json",
  "/private/tmp/ebay_image_audit_50.json",
];
const audits = await Promise.all(
  auditPaths.map(async (auditPath) =>
    JSON.parse(await fs.readFile(auditPath, "utf8")),
  ),
);
const imageUrlsBySku = Object.fromEntries(
  audits
    .flatMap((audit) => audit.results || [])
    .map((result) => [result.sku, result.image_urls || []]),
);

const pythonCode = [
  "import json, sys",
  "from app.ebay import EbayClient",
  "from app.ebay_draft_batch import inventory_sheet_missing_drafts",
  "images = json.load(sys.stdin)",
  "drafts = inventory_sheet_missing_drafts()",
  "print(json.dumps({'csv': EbayClient._seller_hub_draft_csv(drafts, images), 'drafts': [draft.to_dict() for draft in drafts]}))",
].join("; ");
const generated = JSON.parse(
  execFileSync(".venv/bin/python", ["-c", pythonCode], {
    cwd: projectDir,
    input: JSON.stringify(imageUrlsBySku),
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
  }),
);

await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(csvPath, `\uFEFF${generated.csv}`, "utf8");

const workbook = await Workbook.fromCSV(generated.csv, {
  sheetName: "Seller Hub Drafts",
});
const draftSheet = workbook.worksheets.getItem("Seller Hub Drafts");
draftSheet.showGridLines = false;
draftSheet.freezePanes.freezeRows(3);
draftSheet.getRange("F4:G72").values = generated.drafts.map((draft) => [
  Number(draft.price),
  Number(draft.quantity),
]);
draftSheet.getRange("A1:K2").format = {
  fill: "#E8F0FE",
  font: { color: "#334155", italic: true },
};
draftSheet.getRange("A3:K3").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
draftSheet.getRange("A4:K72").format = {
  verticalAlignment: "top",
  wrapText: true,
};
draftSheet.getRange("F4:F72").format.numberFormat = "$#,##0.00";
draftSheet.getRange("G4:G72").format.numberFormat = "0";
draftSheet.getRange("A1:K72").format.borders = {
  bottom: { style: "thin", color: "#D9E2F3" },
};
draftSheet.getRange("A1:K72").format.autofitColumns();
draftSheet.getRange("A1:K72").format.autofitRows();
draftSheet.getRange("D4:D72").format.columnWidth = 36;
draftSheet.getRange("H4:H72").format.columnWidth = 42;
draftSheet.getRange("J4:J72").format.columnWidth = 58;
draftSheet.getRange("A1:K2").format.rowHeight = 20;
draftSheet.getRange("A3:K3").format.rowHeight = 28;

const summary = workbook.worksheets.add("Summary");
summary.showGridLines = false;
summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [["eBay Seller Hub Visible Drafts"]];
summary.getRange("A1:F1").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF", size: 18 },
  horizontalAlignment: "left",
  verticalAlignment: "center",
  rowHeight: 34,
};
summary.getRange("A2:F2").merge();
summary.getRange("A2").values = [[
  "Prepared from the user inventory sheet and verified eBay API image records. Action is Draft; no publish action is present.",
]];
summary.getRange("A2:F2").format = {
  fill: "#D9EAF7",
  font: { color: "#334155", italic: true },
  wrapText: true,
  rowHeight: 34,
};
summary.getRange("A4:B7").values = [
  ["Metric", "Value"],
  ["Visible draft rows", null],
  ["Inventory units represented", null],
  ["Rows with blank UPC", null],
];
summary.getRange("B5:B7").formulas = [
  ["=COUNTA('Seller Hub Drafts'!A4:A72)"],
  ["=SUM('Seller Hub Drafts'!G4:G72)"],
  ["=COUNTBLANK('Seller Hub Drafts'!E4:E72)"],
];
summary.getRange("D4:E7").values = [
  ["Check", "Result"],
  ["Rows with pictures", null],
  ["Draft action rows", null],
  ["Fixed-price rows", null],
];
summary.getRange("E5:E7").formulas = [
  ["=COUNTA('Seller Hub Drafts'!H4:H72)"],
  ['=COUNTIF(\'Seller Hub Drafts\'!A4:A72,"Draft")'],
  ['=COUNTIF(\'Seller Hub Drafts\'!K4:K72,"FixedPrice")'],
];
for (const headerRange of ["A4:B4", "D4:E4"]) {
  summary.getRange(headerRange).format = {
    fill: "#4472C4",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
  };
}
summary.getRange("A5:A7").format.font = { bold: true, color: "#334155" };
summary.getRange("D5:D7").format.font = { bold: true, color: "#334155" };
summary.getRange("B5:B7").format = {
  fill: "#E2F0D9",
  font: { bold: true, color: "#215E21" },
  horizontalAlignment: "center",
  numberFormat: "0",
};
summary.getRange("E5:E7").format = {
  fill: "#E2F0D9",
  font: { bold: true, color: "#215E21" },
  horizontalAlignment: "center",
  numberFormat: "0",
};
summary.getRange("A9:F9").merge();
summary.getRange("A9").values = [["Operational notes"]];
summary.getRange("A9:F9").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF" },
};
summary.getRange("A10:F13").values = [
  [
    "Draft labels",
    "Each custom label starts with SH- to keep Seller Hub drafts distinct from the unpublished Inventory API offers.",
    null,
    null,
    null,
    null,
  ],
  [
    "UPC",
    "Blank by design because UPC values were not supplied.",
    null,
    null,
    null,
    null,
  ],
  [
    "Condition",
    "NEW is preserved only for the one explicitly new item; open-box and refurbished rows use USED so they can be reviewed in Seller Hub.",
    null,
    null,
    null,
    null,
  ],
  [
    "Publishing",
    "The upload action is Draft. Review physical condition, included accessories, and final photos before publishing.",
    null,
    null,
    null,
    null,
  ],
];
for (let row = 10; row <= 13; row += 1) {
  summary.getRange(`B${row}:F${row}`).merge();
}
summary.getRange("A10:A13").format.font = { bold: true, color: "#17365D" };
summary.getRange("A10:F13").format = {
  wrapText: true,
  verticalAlignment: "top",
};
summary.getRange("A15:F15").merge();
summary.getRange("A15").values = [["Sources"]];
summary.getRange("A15:F15").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF" },
};
summary.getRange("A16:F18").values = [
  [
    "Inventory",
    "/Users/seanfouz/Downloads/INVENTORY FOR WALMART.xls",
    null,
    null,
    null,
    null,
  ],
  [
    "Images",
    "Verified from eBay Inventory API records for all 69 unpublished offers.",
    null,
    null,
    null,
    null,
  ],
  [
    "Template guide",
    "https://pages.ebay.com/sh/reports/help/uploadable-file-feeds/",
    null,
    null,
    null,
    null,
  ],
];
for (let row = 16; row <= 18; row += 1) {
  summary.getRange(`B${row}:F${row}`).merge();
}
summary.getRange("A16:A18").format.font = { bold: true, color: "#17365D" };
summary.getRange("A1:F18").format.borders = {
  bottom: { style: "thin", color: "#D9E2F3" },
};
summary.getRange("A1:F18").format.autofitColumns();
summary.getRange("A1:F18").format.autofitRows();
summary.getRange("A1:A18").format.columnWidth = 24;
summary.getRange("B1:F18").format.columnWidth = 18;
summary.freezePanes.freezeRows(2);

const inspection = await workbook.inspect({
  kind: "sheet,formula",
  maxChars: 5000,
  tableMaxRows: 8,
  tableMaxCols: 8,
});
const errorInspection = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  maxChars: 3000,
});
const preview = await workbook.render({
  sheetName: "Summary",
  autoCrop: "all",
  scale: 1.25,
  format: "png",
});
await fs.writeFile(
  previewPath,
  new Uint8Array(await preview.arrayBuffer()),
);
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(xlsxPath);

console.log(
  JSON.stringify(
    {
      csvPath,
      xlsxPath,
      previewPath,
      rows: generated.drafts.length,
      units: generated.drafts.reduce((sum, draft) => sum + draft.quantity, 0),
      inspection,
      errorInspection,
    },
    null,
    2,
  ),
);
