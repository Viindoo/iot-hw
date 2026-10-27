import assert from "node:assert/strict";
import { test } from "node:test";

import { loadStandalone } from "./load.mjs";

const { documentationUrl } = await loadStandalone("links.js");

const GUIDE = "https://viindoo.com/documentation";
const CHAPTER = "applications/internet-of-things.html";

test("English page of a box of series 18.0 opens the English chapter of that series", () => {
    assert.equal(documentationUrl("18.0", "en"), `${GUIDE}/18.0/${CHAPTER}`);
});

test("Vietnamese page of a box of series 18.0 opens the Vietnamese chapter, the language after the series", () => {
    assert.equal(documentationUrl("18.0", "vi"), `${GUIDE}/18.0/vi/${CHAPTER}`);
});

test("the series is the one the box reports, not one written in the page", () => {
    assert.equal(documentationUrl("19.0", "en"), `${GUIDE}/19.0/${CHAPTER}`);
});

test("a box whose series is not known yet opens the guide at its start, whatever the language", () => {
    assert.equal(documentationUrl("", "en"), GUIDE);
    assert.equal(documentationUrl("", "vi"), GUIDE);
    assert.equal(documentationUrl(undefined, "vi"), GUIDE);
});

test("no address of the App Store is built", () => {
    for (const [series, language] of [["18.0", "en"], ["18.0", "vi"], ["", "en"]]) {
        assert.doesNotMatch(documentationUrl(series, language), /\/apps\/modules\//);
    }
});
