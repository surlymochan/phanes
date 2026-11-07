import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const ROOT = process.cwd();

function readRepoFile(path) {
  return readFileSync(resolve(ROOT, path), 'utf8');
}

describe('coverage config', () => {
  test('package exposes a coverage script backed by c8', () => {
    const pkg = JSON.parse(readRepoFile('package.json'));

    assert.equal(pkg.scripts.coverage, 'c8 node scripts/run-tests.mjs');
    assert.ok(pkg.devDependencies.c8, 'c8 must be a devDependency');
  });

  test('.c8rc.json covers src and bin, excludes tests', () => {
    const configPath = resolve(ROOT, '.c8rc.json');
    assert.ok(existsSync(configPath), '.c8rc.json must exist');

    const config = JSON.parse(readRepoFile('.c8rc.json'));
    assert.ok(config.include.includes('src/**/*.js'));
    assert.ok(config.include.includes('bin/**/*.js'));
    assert.ok(config.exclude.includes('test/**'));
    assert.equal(config['reports-dir'], './coverage');
  });

  test('.gitignore keeps coverage output out of the repo', () => {
    const gitignore = readRepoFile('.gitignore');

    assert.match(gitignore, /^coverage\/$/m);
  });
});
