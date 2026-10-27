/* The one place for the web addresses and contacts the page shows. */

export const SITE = "https://viindoo.com";
export const SUPPORT_URL = `${SITE}/ticket`;
const IOT_CHAPTER = "applications/internet-of-things.html";

/*
 * The chapter of the user guide on the IoT box, for the series the box reports and the language of the page, so that
 * no text is edited at a forward-port. The guide puts the language after the series: English has no segment,
 * Vietnamese has `vi`. Without a series the guide is opened at its start.
 */
export function documentationUrl(series, language) {
    if (!series) {
        return `${SITE}/documentation`;
    }
    const segment = language === "vi" ? "/vi" : "";
    return `${SITE}/documentation/${encodeURIComponent(series)}${segment}/${IOT_CHAPTER}`;
}
