/** Author requested values with Artifact Tool, then use preserve_xlsx.py to publish. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';

const argv = process.argv.slice(2);
const configPath = argv[0];
const option = name => {
  const index = argv.indexOf(name);
  return index < 0 ? undefined : argv[index + 1];
};
const workDir = option('--work-dir');
const python = option('--python');
if (!configPath || !workDir || !python) {
  throw Error('Usage: node workbook.mjs config.json --work-dir <new scratch directory> --python <bundled Python> [--verify]');
}
const adapter = path.join(path.dirname(fileURLToPath(import.meta.url)), 'preserve_xlsx.py');
const preparedPath = path.join(workDir, 'prepared.json');
const verify = argv.includes('--verify');
if (!verify) {
  const preflight = spawnSync(python, [adapter, '--prepare', configPath, '--work-dir', workDir],
    {encoding: 'utf8', windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  if (preflight.status !== 0) throw Error(preflight.stderr || preflight.error?.message || 'Workbook preflight failed');
}
const config = JSON.parse((await fs.readFile(preparedPath, 'utf8')).replace(/^\uFEFF/, ''));
// Resolve only through the caller's scratch junction to the bundled dependencies.
const require = createRequire(path.join(path.resolve(workDir), 'package.json'));
const {FileBlob, SpreadsheetFile} = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const report = verify ? JSON.parse(await fs.readFile(path.join(workDir, 'verification.json'), 'utf8')) : null;
const outputHash = async () => createHash('sha256').update(await fs.readFile(report.output)).digest('hex');
if (verify && await outputHash() !== report.sha256) throw Error('Output hash changed before Artifact verification');
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(verify ? report.output : config.snapshot));

function column(number) {
  let text = '';
  while (number) {
    const remainder = (number - 1) % 26;
    text = String.fromCharCode(65 + remainder) + text;
    number = Math.floor((number - 1) / 26);
  }
  return text;
}

function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

function checkHeaders() {
  for (const [name, headers] of Object.entries(config.headers)) {
    const got = wb.worksheets.getItem(name).getRange(`A1:${column(headers.length)}1`).values[0];
    if (!same(got, headers)) throw Error(`Artifact header mismatch: ${name}`);
  }
}

const previews = config.previews ?? (config.operation === 'batch'
  ? Object.keys(config.sheets).map(sheet => ({sheet, range: 'A1:F5'}))
  : [...new Map(config.changes.map(change => {
      const row = Number(change.cell.match(/\d+$/)[0]);
      const col = change.cell.match(/^[A-Z]+/)[0];
      return [`${change.sheet}:${col}`, {sheet: change.sheet, range: `${col}${Math.max(1, row - 1)}:${col}${row + 2}`}];
    })).values()].slice(0, 6));

async function render(stage) {
  for (const [index, preview] of previews.entries()) {
    const blob = await wb.render({sheetName: preview.sheet, range: preview.range, scale: 1, format: 'png'});
    await fs.writeFile(path.join(workDir, `${stage}_${index + 1}.png`), new Uint8Array(await blob.arrayBuffer()), {flag: 'wx'});
  }
}

function checkValues() {
  if (config.operation === 'batch') {
    for (const [name, spec] of Object.entries(config.sheets)) {
      const got = wb.worksheets.getItem(name).getRange(`A2:${column(spec.rows[0].length)}${spec.rows.length + 1}`).values;
      if (!same(got, spec.rows)) throw Error(`Artifact values differ: ${name}`);
    }
  } else {
    for (const {sheet, cell, value} of config.changes) {
      if (!same(wb.worksheets.getItem(sheet).getRange(cell).values[0][0], value)) {
        throw Error(`Artifact values differ: ${sheet}!${cell}`);
      }
    }
  }
}

checkHeaders();
if (!verify) {
  await render('before');
  if (config.operation === 'batch') {
    for (const [name, spec] of Object.entries(config.sheets)) {
      const sheet = wb.worksheets.getItem(name);
      const last = column(spec.rows[0].length);
      // Only intermediate needs styles for previews; native seed styles are preserved on publication.
      for (let index = 0; index < spec.rows.length; index++) {
        const row = index + 2;
        const seed = spec.seed_rows[index % spec.seed_rows.length];
        sheet.getRange(`A${row}:${last}${row}`).copyFrom(sheet.getRange(`A${seed}:${last}${seed}`), 'all');
      }
      sheet.getRange(`A2:${last}${spec.rows.length + 1}`).values = spec.rows;
    }
  } else {
    for (const {sheet, cell, value} of config.changes) {
      wb.worksheets.getItem(sheet).getRange(cell).values = [[value]];
    }
  }
  wb.recalculate();
  checkValues();
  console.log((await wb.inspect({kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!',
    options: {useRegex: true, maxResults: 10}, maxChars: 1600})).ndjson);
  await fs.access(config.authored).then(() => { throw Error('Intermediate exists; use a new work directory'); }, error => {
    if (error.code !== 'ENOENT') throw error;
  });
  await (await SpreadsheetFile.exportXlsx(wb)).save(config.authored);
  await render('authored');
  console.log(JSON.stringify({authored: config.authored, next: `Run preserve_xlsx.py --finish with ${preparedPath}`}));
} else {
  checkValues();
  await render('final');
  const verifiedHash = await outputHash();
  if (verifiedHash !== report.sha256) throw Error('Output hash changed during Artifact verification');
  await fs.writeFile(path.join(workDir, 'verification.json'), JSON.stringify({
    ...report, artifact_verified: true, artifact_sha256: verifiedHash,
  }, null, 2), 'utf8');
  console.log(JSON.stringify({verified: report.output, requestedValuesMatch: true}));
}
