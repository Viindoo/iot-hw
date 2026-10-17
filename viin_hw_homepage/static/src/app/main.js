/* Starts the box page: loads the texts, picks the language, mounts the application. */

import { Homepage } from "./Homepage.js";
import { initialLanguage, loadTexts, setLanguage } from "./i18n.js";
import { refresh } from "./poll.js";
import { store } from "./store.js";

const { mount } = owl;

async function start() {
    await loadTexts();
    const language = initialLanguage();
    setLanguage(language);
    store.language = language;
    await mount(Homepage, document.body, { env: { refresh }, dev: new URLSearchParams(window.location.search).has("debug") });
}

start().catch((error) => {
    console.error("The box page could not start", error);
    document.body.textContent = "";
    const message = document.createElement("p");
    message.style.cssText = "margin:2rem;font-family:system-ui,sans-serif";
    message.textContent = "Viindoo IoT Box: this page could not start. Reload it; if this stays, restart the box. / Không mở được trang. Hãy tải lại trang; nếu vẫn lỗi, hãy khởi động lại hộp.";
    document.body.append(message);
});
