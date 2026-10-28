// Owned regression cases. They supplement, and never replace, publisher SDK cases.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, symlinkSync, readlinkSync, chmodSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

const packageRoot = process.env.OWNED_GLOB_PARENT_ROOT;
assert.ok(packageRoot);
const requireParent = createRequire(join(packageRoot, 'package.json'));
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'owned-glob-parent-case-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}
function write(root, path, value = path) {
  const target = join(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, value);
  return target;
}
async function load(name, dialect) {
  const cjs = requireParent.resolve(name);
  assert.ok(cjs.includes('/dist/commonjs/'));
  return dialect === 'commonjs' ? requireParent(name) : import(pathToFileURL(cjs.replace('/dist/commonjs/', '/dist/esm/')).href);
}

for (const dialect of ['esm', 'commonjs']) {
  const resolver = await load('resolve-import', dialect);
  const remover = await load('rimraf', dialect);
  const synchronizer = await load('sync-content', dialect);
  const braces = await load('brace-expansion', dialect);
  const matcher = await load('minimatch', dialect);
  const walker = await load('glob', dialect);

  for (const sync of [false, true]) {
    const kind = sync ? 'sync' : 'async';
    test(`${dialect} resolver ${kind} expands nested wildcard exports into exact URLs`, async t => {
      const root = fixture(t);
      write(root, 'lib/alpha.js');
      write(root, 'lib/nested/beta.js');
      write(root, 'lib/unrelated.txt');
      const manifest = write(root, 'package.json', JSON.stringify({ name: 'owned-export-fixture', type: 'module', exports: { './features/*': './lib/*.js' } }));
      const result = sync ? resolver.resolveAllExportsSync(manifest) : await resolver.resolveAllExports(manifest);
      assert.deepEqual(Object.keys(result).sort(), ['./features/alpha', './features/nested/beta']);
      assert.equal(result['./features/alpha'].href, pathToFileURL(join(root, 'lib/alpha.js')).href);
      assert.equal(result['./features/nested/beta'].href, pathToFileURL(join(root, 'lib/nested/beta.js')).href);
    });

    test(`${dialect} resolver ${kind} preserves blocked exports and rejects an invalid package`, async t => {
      const root = fixture(t);
      write(root, 'lib/visible.js');
      const manifest = write(root, 'package.json', JSON.stringify({ name: 'owned-export-fixture', exports: { './visible': './lib/visible.js', './blocked': null } }));
      const result = sync ? resolver.resolveAllExportsSync(manifest) : await resolver.resolveAllExports(manifest);
      assert.deepEqual(Object.keys(result), ['./visible']);
      writeFileSync(manifest, '{');
      if (sync) assert.throws(() => resolver.resolveAllExportsSync(manifest));
      else await assert.rejects(() => resolver.resolveAllExports(manifest));
    });

    test(`${dialect} rimraf ${kind} applies glob selection while retaining unmatched files`, async t => {
      const root = fixture(t);
      write(root, 'remove/a.tmp');
      write(root, 'remove/nested/b.tmp');
      const keep = write(root, 'remove/keep.txt', 'retain');
      const pattern = join(root, 'remove/**/*.tmp');
      const result = sync ? remover.rimrafSync(pattern, { glob: true }) : await remover.rimraf(pattern, { glob: true });
      assert.equal(result, true);
      assert.equal(existsSync(join(root, 'remove/a.tmp')), false);
      assert.equal(existsSync(join(root, 'remove/nested/b.tmp')), false);
      assert.equal(readFileSync(keep, 'utf8'), 'retain');
    });

    test(`${dialect} rimraf ${kind} removes a symlink without deleting its external target`, async t => {
      const root = fixture(t);
      const target = write(root, 'outside/keep.txt', 'retain-external');
      mkdirSync(join(root, 'remove'));
      symlinkSync(join(root, 'outside'), join(root, 'remove/link'), 'dir');
      const result = sync ? remover.rimrafSync(join(root, 'remove')) : await remover.rimraf(join(root, 'remove'));
      assert.equal(result, true);
      assert.equal(existsSync(join(root, 'remove')), false);
      assert.equal(readFileSync(target, 'utf8'), 'retain-external');
    });

    test(`${dialect} sync-content ${kind} copies nested content, modes and symlinks and removes stale files`, async t => {
      const root = fixture(t);
      write(root, 'from/nested/value.txt', 'new-value');
      const executable = write(root, 'from/tool.sh', '#!/bin/sh\nexit 0\n');
      chmodSync(executable, 0o750);
      symlinkSync('nested/value.txt', join(root, 'from/link'));
      write(root, 'to/nested/value.txt', 'old-value');
      write(root, 'to/stale.txt', 'remove-stale');
      if (sync) synchronizer.syncContentSync(join(root, 'from'), join(root, 'to'));
      else await synchronizer.syncContent(join(root, 'from'), join(root, 'to'));
      assert.equal(readFileSync(join(root, 'to/nested/value.txt'), 'utf8'), 'new-value');
      assert.equal(readFileSync(join(root, 'to/tool.sh'), 'utf8'), '#!/bin/sh\nexit 0\n');
      assert.equal(statSync(join(root, 'to/tool.sh')).mode & 0o777, 0o750);
      assert.equal(readlinkSync(join(root, 'to/link')), 'nested/value.txt');
      assert.equal(existsSync(join(root, 'to/stale.txt')), false);
    });

    test(`${dialect} sync-content ${kind} rejects related source/destination directories`, async t => {
      const root = fixture(t);
      write(root, 'from/keep.txt', 'preserve');
      if (sync) assert.throws(() => synchronizer.syncContentSync(join(root, 'from'), join(root, 'from/child')), /cannot copy directory into itself or its parent/);
      else await assert.rejects(() => synchronizer.syncContent(join(root, 'from'), join(root, 'from/child')), /cannot copy directory into itself or its parent/);
      assert.equal(readFileSync(join(root, 'from/keep.txt'), 'utf8'), 'preserve');
    });
  }

  test(`${dialect} brace expansion preserves nested alternatives and escaped literals`, () => {
    assert.deepEqual(braces.expand('a{b,c{d,e}}f'), ['abf', 'acdf', 'acef']);
    assert.deepEqual(braces.expand('a{b,c}{d,e}f'), ['abdf', 'abef', 'acdf', 'acef']);
    assert.deepEqual(braces.expand(String.raw`x\{a,b\}y`), ['x{a,b}y']);
    assert.deepEqual(braces.expand('plain.txt'), ['plain.txt']);
  });

  test(`${dialect} brace expansion preserves descending padded and stepped ranges`, () => {
    assert.deepEqual(braces.expand('x{03..01}y'), ['x03y', 'x02y', 'x01y']);
    assert.deepEqual(braces.expand('x{a..e..2}y'), ['xay', 'xcy', 'xey']);
    assert.deepEqual(braces.expand('x{5..1..2}y'), ['x5y', 'x3y', 'x1y']);
  });

  test(`${dialect} minimatch preserves brace/globstar, dot and negated matching`, () => {
    assert.equal(matcher.minimatch('src/nested/a.ts', 'src/**/{a,b}.{ts,js}'), true);
    assert.equal(matcher.minimatch('src/nested/c.ts', 'src/**/{a,b}.{ts,js}'), false);
    assert.equal(matcher.minimatch('src/.hidden.ts', 'src/**/*.ts'), false);
    assert.equal(matcher.minimatch('src/.hidden.ts', 'src/**/*.ts', { dot: true }), true);
    assert.equal(matcher.minimatch('src/a.ts', '!**/*.js'), true);
    assert.equal(matcher.minimatch('src/a.js', '!**/*.js'), false);
  });

  test(`${dialect} glob preserves brace enumeration, dot policy and Path objects`, async t => {
    const root = fixture(t);
    write(root, 'lib/alpha.js');
    write(root, 'src/nested/beta.ts');
    write(root, 'src/.hidden.js');
    write(root, 'other/keep.txt');
    const pattern = '{lib,src}/**/*.{js,ts}';
    const options = { cwd: root, nodir: true };
    const expected = ['lib/alpha.js', 'src/nested/beta.ts'];
    assert.deepEqual((await walker.glob(pattern, options)).sort(), expected);
    assert.deepEqual(walker.globSync(pattern, options).sort(), expected);
    const paths = await walker.glob(pattern, { ...options, withFileTypes: true });
    assert.deepEqual(paths.map(path => path.relativePosix()).sort(), expected);
    assert.deepEqual((await walker.glob(pattern, { ...options, dot: true })).sort(), ['lib/alpha.js', 'src/.hidden.js', 'src/nested/beta.ts']);
  });
}
