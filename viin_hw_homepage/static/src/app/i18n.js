/* English and Vietnamese texts: one JSON file per language, the same keys in both. */

export const LANGUAGES = ["en", "vi"];
const STORAGE_KEY = "viin_box_language";
const dictionaries = {};
let current = "en";

function remembered() {
    try {
        return window.localStorage.getItem(STORAGE_KEY);
    } catch {
        return null;
    }
}

/* The language the person chose before, else the one of the browser. */
export function initialLanguage() {
    const chosen = remembered();
    if (LANGUAGES.includes(chosen)) {
        return chosen;
    }
    const preferred = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language]);
    for (const language of preferred) {
        const code = String(language || "").slice(0, 2).toLowerCase();
        if (LANGUAGES.includes(code)) {
            return code;
        }
    }
    return "en";
}

export async function loadTexts() {
    await Promise.all(LANGUAGES.map(async (language) => {
        const response = await fetch(new URL(`./i18n/${language}.json`, import.meta.url), { cache: "no-cache" });
        if (!response.ok) {
            throw new Error(`texts ${language}: ${response.status}`);
        }
        dictionaries[language] = await response.json();
    }));
}

export function language() {
    return current;
}

export function setLanguage(language) {
    current = LANGUAGES.includes(language) ? language : "en";
    document.documentElement.lang = current;
    try {
        window.localStorage.setItem(STORAGE_KEY, current);
    } catch {
        /* The choice then lasts for this visit only. */
    }
}

export function has(key) {
    return key in (dictionaries[current] || {}) || key in (dictionaries.en || {});
}

/* The text of `key` in the current language, `{name}` replaced from `params`; the key itself when no text has it. */
export function t(key, params) {
    const text = (dictionaries[current] || {})[key] ?? (dictionaries.en || {})[key];
    if (text === undefined) {
        console.warn("No text for", key);
        return key;
    }
    return params ? text.replace(/\{(\w+)\}/g, (found, name) => (name in params ? String(params[name]) : found)) : text;
}

export function when(iso) {
    if (!iso) {
        return "";
    }
    const moment = new Date(iso);
    return Number.isNaN(moment.getTime()) ? String(iso) : moment.toLocaleString(current, { dateStyle: "medium", timeStyle: "short" });
}

export function day(iso) {
    if (!iso) {
        return "";
    }
    const moment = new Date(iso);
    return Number.isNaN(moment.getTime()) ? String(iso) : moment.toLocaleDateString(current, { dateStyle: "medium" });
}

/* A moment to the second in UTC, for the comparison of two clocks. */
export function exact(iso) {
    if (!iso) {
        return "";
    }
    const moment = new Date(iso);
    return Number.isNaN(moment.getTime())
        ? String(iso)
        : moment.toLocaleString(current, { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" });
}
