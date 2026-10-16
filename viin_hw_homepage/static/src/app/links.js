/* The one place for the web addresses and contacts the page shows. */

export const SITE = "https://viindoo.com";
export const SUPPORT_EMAIL = "apps.support@viindoo.com";
export const IOT_MODULE = "viin_iot";

/* The page of the IoT app on Viindoo's app store, for the series the box reports, so that no text is edited at a forward-port. */
export function documentationUrl(series) {
    return series ? `${SITE}/apps/modules/${encodeURIComponent(series)}/${IOT_MODULE}` : SITE;
}
