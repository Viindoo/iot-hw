/* Software: the release the box runs, and the update. */

import { call } from "../api.js";
import { t, when } from "../i18n.js";
import { reasonText } from "../answers.js";
import { sentence, todo, STATE_CLASS } from "../selftext.js";
import { setNotice, store } from "../store.js";

const { Component, xml, useState } = owl;

export class SoftwarePanel extends Component {
    static props = {};
    static template = xml`
        <section class="viin-card" aria-labelledby="software-title" id="software">
            <div class="viin-card-head">
                <h2 id="software-title" t-esc="t('software.title')"/>
            </div>
            <div class="viin-card-body">
                <dl class="viin-rows">
                    <div class="viin-row">
                        <dt t-esc="t('software.release')"/>
                        <dd>
                            <span t-esc="software.release"/>
                            <span t-if="software.candidate_banner" class="badge text-bg-danger ms-1" t-esc="t('software.candidate_tag')"/>
                        </dd>
                    </div>
                    <div class="viin-row" t-if="last and last.at"><dt t-esc="t('software.checked')"/><dd t-esc="when(last.at)"/></div>
                </dl>
                <div t-attf-class="alert alert-{{ alertClass }} mt-3 mb-3" role="status" aria-live="polite">
                    <div t-esc="sentence(line)"/>
                    <div t-if="todo(line)" class="mt-1"><strong><t t-esc="t('selftest.todo')"/></strong> <t t-esc="todo(line)"/></div>
                </div>
                <div t-if="rolledBack" class="alert alert-warning" t-esc="t('software.rolled_back', {release: rolledBack})"/>
                <div class="viin-actions mb-3">
                    <button type="button" class="btn btn-primary" t-if="software.offered" t-on-click="apply" t-att-disabled="busy">
                        <i class="fa fa-download" aria-hidden="true"/><t t-esc="t('software.update_to', {release: software.offered.release})"/>
                    </button>
                    <button type="button" class="btn btn-outline-primary" t-on-click="check" t-att-disabled="busy">
                        <i t-attf-class="fa fa-refresh {{ software.busy === 'checking' ? 'fa-spin' : '' }}" aria-hidden="true"/>
                        <t t-esc="software.busy === 'checking' ? t('software.checking') : t('software.check')"/>
                    </button>
                </div>
                <div class="form-check form-switch">
                    <input class="form-check-input" type="checkbox" id="auto-update" t-att-checked="software.auto_update" t-on-change="toggleAuto"/>
                    <label class="form-check-label" for="auto-update" t-esc="t('software.auto')"/>
                </div>
                <p class="viin-muted small mt-1 mb-0" t-esc="t('software.auto_help')"/>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.when = when;
        this.sentence = sentence;
        this.todo = todo;
        this.store = useState(store);
    }

    get software() {
        return this.store.status.software;
    }

    get line() {
        return this.store.status.selftest.lines.find((line) => line.id === "software");
    }

    get last() {
        return this.software.last_check;
    }

    get busy() {
        return Boolean(this.software.busy);
    }

    get alertClass() {
        return STATE_CLASS[this.line.state] || "secondary";
    }

    get rolledBack() {
        const back = this.software.rolled_back;
        return back && typeof back === "object" ? back.release : "";
    }

    async check() {
        const answer = await call("/viin_hw/update", { method: "POST", params: { action: "check" } });
        if (answer.status !== "started") {
            setNotice("danger", reasonText("update", answer.reason));
        }
        await this.env.refresh();
    }

    async apply() {
        const answer = await call("/viin_hw/update", { method: "POST", params: { action: "apply" } });
        if (answer.status === "started") {
            store.restarting = { kind: "update" };
        } else if (answer.status !== "locked") {
            setNotice("danger", reasonText("update", answer.reason));
        }
    }

    async toggleAuto(event) {
        const enabled = event.target.checked;
        const answer = await call("/viin_hw/update", { method: "POST", params: { action: "auto_update", enabled } });
        if (answer.status !== "saved") {
            event.target.checked = !enabled;
            if (answer.status !== "locked") {
                setNotice("danger", reasonText("update", answer.reason));
            }
        }
        await this.env.refresh();
    }
}
