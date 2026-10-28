// Owned isolated feature probes; never a substitute for TypeDoc's original SDK suites.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';
import { API, SignatureKind } from 'typescript/unstable/sync';
import { isFunctionDeclaration } from 'typescript/unstable/ast/is';

const content = `/** A documented user. */
export interface User { id: number; label?: string; }
/** A result envelope. */
export type Result<T> = { value: T; ok: true };
/** Double a number. */
export function double(value: number): number { return value * 2; }
`;
assert.equal(ts.version, '7.0.2');
writeFileSync('actual-native-api-surface.json', JSON.stringify({
  package: 'typescript', version: ts.version, rootExports: Object.keys(ts).sort(),
  rootCreateProgram: typeof ts.createProgram,
  nativeAPIEntryPoint: 'typescript/unstable/sync',
  publisherLabelsAPIUnstable: true,
  productionOrFullTypeDocSDKAcceptance: false,
}, null, 2) + '\n');

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'owned-native-type-api-'));
  const file = join(directory, 'index.ts');
  const config = join(directory, 'tsconfig.json');
  writeFileSync(file, content);
  writeFileSync(config, JSON.stringify({ compilerOptions: { strict: true, target: 'ESNext', module: 'NodeNext', moduleResolution: 'NodeNext', noEmit: true }, files: ['./index.ts'] }));
  let api;
  const snapshots = [];
  t.after(() => {
    try {
      for (const snapshot of snapshots) if (!snapshot.isDisposed()) snapshot.dispose();
    } finally {
      try { api?.close(); } finally { rmSync(directory, { recursive: true, force: true }); }
    }
  });
  api = new API({ cwd: directory });
  const snapshot = api.updateSnapshot({ openProjects: [config] });
  snapshots.push(snapshot);
  const project = snapshot.getProject(config);
  assert.ok(project, 'Native compiler must load the actual tsconfig');
  const source = project.program.getSourceFile(file);
  assert.ok(source, 'Native compiler must supply a real source AST');
  return { directory, file, config, api, snapshot, snapshots, project, source };
}

test('native API parses real configuration and reports a valid strict project', t => {
  const f = fixture(t);
  const parsed = f.api.parseConfigFile(f.config);
  assert.deepEqual(parsed.fileNames, [f.file]);
  assert.equal(parsed.options.strict, true);
  assert.equal(f.project.rootFiles.includes(f.file), true);
  assert.deepEqual(f.project.program.getSyntacticDiagnostics(f.file), []);
  assert.deepEqual(f.project.program.getSemanticDiagnostics(f.file), []);
});

test('native checker supplies real exports and documentation comments', t => {
  const f = fixture(t);
  const module = f.project.checker.getSymbolAtLocation(f.source);
  assert.ok(module);
  const exports = f.project.checker.getExportsOfModule(module);
  assert.deepEqual(exports.map(symbol => symbol.name).sort(), ['Result', 'User', 'double']);
  const symbol = exports.find(symbol => symbol.name === 'double');
  assert.match(symbol.getDocumentationComment(f.project.checker), /Double a number/);
});

test('native checker preserves interface properties and function signatures', t => {
  const f = fixture(t);
  const module = f.project.checker.getSymbolAtLocation(f.source);
  assert.ok(module);
  const exports = f.project.checker.getExportsOfModule(module);
  const user = exports.find(symbol => symbol.name === 'User');
  const type = f.project.checker.getDeclaredTypeOfSymbol(user);
  assert.deepEqual(f.project.checker.getPropertiesOfType(type).map(symbol => symbol.name).sort(), ['id', 'label']);
  const double = exports.find(symbol => symbol.name === 'double');
  const fnType = f.project.checker.getTypeOfSymbolAtLocation(double, f.source);
  const signatures = f.project.checker.getSignaturesOfType(fnType, SignatureKind.Call);
  assert.equal(signatures.length, 1);
  assert.deepEqual(signatures[0].getParameters().map(symbol => symbol.name), ['value']);
  assert.equal(f.project.checker.typeToString(f.project.checker.getReturnTypeOfSignature(signatures[0])), 'number');
});

test('native source AST and printer retain the real function declaration', t => {
  const f = fixture(t);
  const declaration = f.source.statements.find(node => isFunctionDeclaration(node) && node.name?.text === 'double');
  assert.ok(declaration);
  const printed = f.project.emitter.printNode(declaration);
  assert.match(printed, /function double\(value: number\): number/);
  assert.match(printed, /value \* 2/);
});

test('native semantic diagnostics reject a real invalid typed declaration', t => {
  const f = fixture(t);
  writeFileSync(f.file, content + '\nexport const invalid: number = "not-a-number";\n');
  const updated = f.api.updateSnapshot({ fileChanges: { changed: [f.file] } });
  f.snapshots.push(updated);
  const project = updated.getProject(f.config);
  assert.ok(project);
  assert.equal(project.program.getSemanticDiagnostics(f.file).some(diagnostic => diagnostic.code === 2322), true);
});

test('native snapshots preserve prior diagnostics and explicitly dispose handles', t => {
  const f = fixture(t);
  assert.deepEqual(f.project.program.getSemanticDiagnostics(f.file), []);
  writeFileSync(f.file, content + '\nexport const invalid: number = "not-a-number";\n');
  const updated = f.api.updateSnapshot({ fileChanges: { changed: [f.file] } });
  f.snapshots.push(updated);
  const project = updated.getProject(f.config);
  assert.ok(project);
  assert.equal(project.program.getSemanticDiagnostics(f.file).some(diagnostic => diagnostic.code === 2322), true);
  assert.deepEqual(f.project.program.getSemanticDiagnostics(f.file), []);
  f.snapshot.dispose();
  assert.equal(f.snapshot.isDisposed(), true);
  assert.throws(() => f.snapshot.getProjects(), /disposed/i);
  assert.equal(project.program.getSemanticDiagnostics(f.file).some(diagnostic => diagnostic.code === 2322), true);
});
