// Loads a file of the box page that imports nothing, as a module of its own: the files are served to a browser as
// modules, and a test must not depend on how this version of node tells a module from a script.
import { readFile } from "node:fs/promises";

export async function loadStandalone(relativePath) {
    const source = await readFile(new URL(`../src/app/${relativePath}`, import.meta.url), "utf8");
    if (/^\s*import\s/m.test(source)) {
        throw new Error(`${relativePath} imports another file: a test cannot load it alone`);
    }
    return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}
