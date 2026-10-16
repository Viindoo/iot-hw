/* The self-test at the top of the page: one line when all is well, one instruction per line that is not. */
/* global owl */

import { t, when } from "../i18n.js";
import { sentence, STATE_CLASS, STATE_ICON, stateWord, todo, lineName } from "../selftext.js";
import { pairAgain } from "../pairing.js";
import { openDialog, store } from "../store.js";

const { Component, xml, useState } = owl;

const OK_STATES = ["ok", "neutral"];

/* The button a line may offer for what it asks the installer to do: [label key, what it opens]. */
const BUTTONS = {
    set_clock_from_server: ["button.set_clock", "clock"],
    pair_box: ["button.connect", "pair"],
    pair_again: ["button.pair_again", "pair"],
    connect_network: ["button.wifi", "wifi"],
    join_wifi: ["button.wifi", "wifi"],
    use_own_certificate: ["button.own_certificate", "certificate"],
    renew_own_certificate: ["button.own_certificate", "certificate"],
    restart_box: ["button.restart", "restart"],
};

export class SelfTest extends Component {
    static props = {};
    static template = xml`
        <section class="viin-card" aria-labelledby="selftest-title">
            <div class="viin-card-head">
                <h2 id="selftest-title" t-esc="t('selftest.title')"/>
                <div class="viin-actions">
                    <button type="button" class="btn btn-outline-primary btn-sm" t-on-click="look" t-att-disabled="state.looking">
                        <i t-attf-class="fa fa-refresh {{ state.looking ? 'fa-spin' : '' }}"/> <t t-esc="t('selftest.again')"/>
                    </button>
                    <a class="btn btn-outline-primary btn-sm" href="/viin_hw/diagnostic">
                        <i class="fa fa-download"/> <t t-esc="t('selftest.diagnostic')"/>
                    </a>
                </div>
            </div>
            <div class="viin-card-body">
                <t t-if="selftest">
                    <div t-attf-class="alert alert-{{ stateClass(selftest.state) }} viin-summary mb-0" role="status" aria-live="polite">
                        <i t-attf-class="fa fa-lg {{ icon(selftest.state) }}" aria-hidden="true"/>
                        <span class="viin-summary-text" t-esc="summary"/>
                    </div>
                    <div class="mt-3" t-if="notes.length">
                        <div t-foreach="notes" t-as="line" t-key="line.id" class="viin-line">
                            <div class="viin-line-icon"><i class="fa fa-lg fa-info-circle text-info" aria-hidden="true"/></div>
                            <div class="viin-line-main">
                                <div class="viin-line-name" t-esc="lineName(line)"/>
                                <div t-esc="sentence(line)"/>
                            </div>
                        </div>
                    </div>
                    <div class="mt-3" t-if="attention.length">
                        <div t-foreach="attention" t-as="line" t-key="line.id" class="viin-line">
                            <div class="viin-line-icon"><i t-attf-class="fa fa-lg {{ icon(line.state) }}" aria-hidden="true"/></div>
                            <div class="viin-line-main">
                                <div class="viin-line-name">
                                    <t t-esc="lineName(line)"/>
                                    <span t-attf-class="badge text-bg-{{ stateClass(line.state) }} ms-1" t-esc="stateWord(line.state)"/>
                                </div>
                                <div t-esc="sentence(line)"/>
                                <div class="viin-line-todo" t-if="todo(line)">
                                    <strong><t t-esc="t('selftest.todo')"/></strong> <t t-esc="todo(line)"/>
                                </div>
                                <div class="mt-2" t-if="button(line)">
                                    <button type="button" class="btn btn-primary btn-sm" t-on-click="() => this.press(line)" t-esc="t(button(line)[0])"/>
                                </div>
                                <div class="mt-2" t-if="line.action === 'apply_update' or line.action === 'write_new_image'">
                                    <a class="btn btn-primary btn-sm" href="#software" t-esc="t('button.to_software')"/>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="mt-3">
                        <button type="button" class="btn btn-link p-0" t-on-click="toggleAll" t-att-aria-expanded="state.all ? 'true' : 'false'">
                            <i t-attf-class="fa {{ state.all ? 'fa-chevron-up' : 'fa-chevron-down' }}" aria-hidden="true"/>
                            <t t-esc="state.all ? t('selftest.hide_all') : t('selftest.show_all')"/>
                        </button>
                        <div t-if="state.all" class="mt-2">
                            <div t-foreach="selftest.lines" t-as="line" t-key="line.id" class="viin-line">
                                <div class="viin-line-icon"><i t-attf-class="fa {{ icon(line.state) }}" aria-hidden="true"/></div>
                                <div class="viin-line-main">
                                    <span class="viin-line-name" t-esc="lineName(line)"/>:
                                    <span t-esc="sentence(line)"/>
                                </div>
                            </div>
                            <p class="viin-muted small mt-2 mb-0">
                                <t t-esc="t('selftest.at', {time: when(selftest.at)})"/>
                            </p>
                        </div>
                    </div>
                </t>
                <p t-else="" class="mb-0" t-esc="t('selftest.reading')"/>
            </div>
        </section>`;

    setup() {
        this.t = t;
        this.when = when;
        this.sentence = sentence;
        this.todo = todo;
        this.lineName = lineName;
        this.stateWord = stateWord;
        this.store = useState(store);
        this.state = useState({ all: false, looking: false });
    }

    get selftest() {
        return this.store.status && this.store.status.selftest;
    }

    get attention() {
        return this.selftest.lines.filter((line) => !OK_STATES.includes(line.state));
    }

    /* A line that is well but asks the reader for one more acceptance in each browser. */
    get notes() {
        return this.selftest.lines.filter(
            (line) =>
                OK_STATES.includes(line.state) && line.details && line.details.accept_again === true
        );
    }

    get summary() {
        const state = this.selftest.state;
        if (state === "ok") {
            return t("selftest.all_ok");
        }
        const count = this.attention.length;
        return t(`selftest.summary_${state}`, { count });
    }

    stateClass(state) {
        return STATE_CLASS[state] || "secondary";
    }

    icon(state) {
        return STATE_ICON[state] || "fa-info-circle";
    }

    button(line) {
        return BUTTONS[line.action] || null;
    }

    /* A connected box asks for its settings code before the pairing form, as the Server panel does. */
    press(line) {
        const [, name] = BUTTONS[line.action];
        if (name === "pair") {
            pairAgain();
            return;
        }
        openDialog(name, name === "clock" ? { serverUrl: this.store.status.server.address } : {});
    }

    toggleAll() {
        this.state.all = !this.state.all;
    }

    async look() {
        this.state.looking = true;
        try {
            await this.env.refresh(true);
        } finally {
            this.state.looking = false;
        }
    }
}
