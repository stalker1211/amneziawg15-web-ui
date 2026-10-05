// puppeteer for the browser scripts (smoke_*.js, screenshot.js), so each runs as plain
// `node tests/<script>.js` with local Node: Node's own lookup first (a NODE_PATH, or a
// node_modules beside the tree), else the global install (`npm i -g puppeteer`, found
// through `npm root -g`, which Node never searches by itself).
const path = require('path');
const { execFileSync } = require('child_process');

function load() {
    try {
        return require('puppeteer');
    } catch (error) {
        if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
    let root = '';
    try {
        root = execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
        return require(path.join(root, 'puppeteer'));
    } catch (error) {
        if (error.code && error.code !== 'MODULE_NOT_FOUND' && error.code !== 'ENOENT') throw error;
    }
    console.error(`puppeteer not found (nor in ${root || "npm's global root"}): npm i -g puppeteer`);
    process.exit(2);
}

module.exports = load();
