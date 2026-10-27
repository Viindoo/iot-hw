/* The log levels: whether the box log goes to the server, and the level of the box, its software, each interface and each driver. */

import { call, send } from "../../api.js";
import { reasonText } from "../../answers.js";
import { has, t } from "../../i18n.js";
import { closeDialog, store } from "../../store.js";
import { Dialog } from "../Dialog.js";

const { Component, xml, useState, onMounted } = owl;

/* The names under which the log-level route sets a level. */
const PREFIX = "iot-logging-";
const BOX = `${PREFIX}root`;
const SOFTWARE = `${PREFIX}odoo`;
const INTERFACE = `${PREFIX}interface-`;
const DRIVER = `${PREFIX}driver-`;

export class LogLevelDialog extends Component {
    static props = {};
    static components = { Dialog };
    static template = xml`
        <Dialog title="t('loglevel.title')" onClose.bind="close" large="true">
            <p t-if="state.loading"><i class="fa fa-circle-o-notch fa-spin" aria-hidden="true"/><t t-esc="t('loglevel.loading')"/></p>
            <div t-if="state.error" class="alert alert-danger" role="alert" t-esc="state.error"/>
            <div t-if="state.saved" class="alert alert-success" role="status" aria-live="polite" t-esc="t('loglevel.saved')"/>
            <t t-if="state.data">
                <p t-esc="t('loglevel.intro', {recommended: levelName(state.data.recommended_log_level)})"/>
                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" id="log-to-server" t-att-checked="state.data.is_log_to_server_activated" t-on-change="(event) => this.set('log-to-server', event.target.checked)"/>
                    <label class="form-check-label" for="log-to-server" t-esc="state.data.server ? t('loglevel.to_server') : t('loglevel.to_server_none')"/>
                </div>

                <h3 class="viin-subhead" t-esc="t('loglevel.global')"/>
                <div class="row g-2 mb-2 align-items-center">
                    <label class="col-sm-5 col-form-label" for="level-box" t-esc="t('loglevel.box')"/>
                    <div class="col-sm-7">
                        <select id="level-box" class="form-select form-select-sm" t-on-change="(event) => this.set('${BOX}', event.target.value)">
                            <option t-foreach="levels.root" t-as="level" t-key="level" t-att-value="level" t-att-selected="level === state.data.root_logger_log_level" t-esc="levelName(level)"/>
                        </select>
                    </div>
                </div>
                <div class="row g-2 mb-2 align-items-center">
                    <label class="col-sm-5 col-form-label" for="level-software" t-esc="t('loglevel.software')"/>
                    <div class="col-sm-7">
                        <select id="level-software" class="form-select form-select-sm" t-on-change="(event) => this.set('${SOFTWARE}', event.target.value)">
                            <option t-foreach="levels.odoo" t-as="level" t-key="level" t-att-value="level" t-att-selected="level === state.data.odoo_current_log_level" t-esc="levelName(level)"/>
                        </select>
                    </div>
                </div>

                <h3 class="viin-subhead" t-esc="t('loglevel.interfaces')"/>
                <p class="viin-muted small" t-if="!interfaces.length" t-esc="t('loglevel.none')"/>
                <div t-foreach="interfaces" t-as="item" t-key="item.file" class="row g-2 mb-2 align-items-center">
                    <label class="col-sm-5 col-form-label viin-break" t-att-for="'level-i-' + item_index" t-esc="item.name"/>
                    <div class="col-sm-7">
                        <select t-if="item.info" t-att-id="'level-i-' + item_index" class="form-select form-select-sm" t-on-change="(event) => this.set('${INTERFACE}' + item.file, event.target.value)">
                            <option t-foreach="levels.interface" t-as="level" t-key="level" t-att-value="level" t-att-selected="level === item.current" t-esc="levelName(level)"/>
                        </select>
                        <span t-else="" class="viin-muted small" t-esc="t(item.silence)"/>
                    </div>
                </div>

                <h3 class="viin-subhead" t-esc="t('loglevel.drivers')"/>
                <p class="viin-muted small" t-if="!drivers.length" t-esc="t('loglevel.none')"/>
                <div t-foreach="drivers" t-as="item" t-key="item.file" class="row g-2 mb-2 align-items-center">
                    <label class="col-sm-5 col-form-label viin-break" t-att-for="'level-d-' + item_index" t-esc="item.name"/>
                    <div class="col-sm-7">
                        <select t-if="item.info" t-att-id="'level-d-' + item_index" class="form-select form-select-sm" t-on-change="(event) => this.set('${DRIVER}' + item.file, event.target.value)">
                            <option t-foreach="levels.driver" t-as="level" t-key="level" t-att-value="level" t-att-selected="level === item.current" t-esc="levelName(level)"/>
                        </select>
                        <span t-else="" class="viin-muted small" t-esc="t(item.silence)"/>
                    </div>
                </div>
            </t>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-primary" t-on-click="close" t-esc="t('common.close')"/>
            </t>
        </Dialog>`;

    setup() {
        this.t = t;
        this.state = useState({ loading: true, data: null, error: "", saved: false });
        onMounted(() => this.load());
    }

    /* The levels each kind of logger may be given: those the page data offers, else core's own list. */
    get levels() {
        const offered = (store.status && store.layer && store.layer.offered_log_levels) || null;
        const all = this.state.data.available_log_levels;
        return offered || { root: all, odoo: all, interface: [...all, "parent"], driver: [...all, "parent"] };
    }

    levelName(level) {
        return has(`loglevel.level_${level}`) ? t(`loglevel.level_${level}`) : level;
    }

    /* Why a handler file has no level to set: the box did not load it, it loaded it and the file defines no logger,
     * or the box has not finished loading its files yet. */
    silence(kind, file) {
        const handlers = (store.status && store.status.drivers) || {};
        if ((handlers.failed || []).some((failed) => failed.file === `${kind}/${file}`)) {
            return "loglevel.not_loaded";
        }
        return handlers.loaded ? "loglevel.no_logger" : "loglevel.not_started";
    }

    handlers(kind, files, info) {
        return files.map((file) => {
            const found = info[file];
            return {
                file,
                name: file.replace(/\.py$/, ""),
                info: Boolean(found),
                silence: this.silence(kind, file),
                current: found ? (found.is_using_parent_level ? "parent" : found.level) : "",
            };
        });
    }

    get interfaces() {
        return this.handlers("interfaces", this.state.data.interfaces_list || [], this.state.data.interfaces_logger_info || {});
    }

    get drivers() {
        return this.handlers("drivers", this.state.data.drivers_list || [], this.state.data.drivers_logger_info || {});
    }

    async load() {
        const { body } = await send("/hw_posbox_homepage/log_levels");
        if (body && Array.isArray(body.available_log_levels)) {
            this.state.data = body;
        } else {
            this.state.error = reasonText("loglevel", body && body.reason ? body.reason : "box_error");
        }
        this.state.loading = false;
    }

    async set(name, value) {
        this.state.saved = false;
        this.state.error = "";
        const answer = await call("/hw_posbox_homepage/log_levels_update", { method: "POST", params: { name, value } });
        if (answer.status === "success") {
            this.state.saved = true;
            await this.load();
        } else {
            if (answer.status !== "locked") {
                this.state.error = reasonText("loglevel", answer.reason || "failed");
            }
            await this.load();
        }
    }

    close() {
        closeDialog();
    }
}
