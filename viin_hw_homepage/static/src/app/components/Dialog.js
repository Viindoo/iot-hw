/* The frame of every dialog: a title, a body, buttons, and a close button that is always there. */
/* global owl */

import { t } from "../i18n.js";

const { Component, xml, onMounted, onWillUnmount, useExternalListener, useRef } = owl;

const RESTORE_TRIES = 50;
const RESTORE_EVERY = 100;

/* Where focus was last put on a control. A control that becomes disabled while it has focus hands the focus to the page
   without any event, so this is how a dialog opened by such a control still finds it. Cleared when the person moves
   the focus away from an enabled control. */
let lastFocused = null;
document.addEventListener("focusin", (event) => {
    lastFocused = event.target;
});
document.addEventListener("focusout", (event) => {
    if (event.target === lastFocused && !event.target.disabled) {
        lastFocused = null;
    }
});

/* Per open dialog frame: the control that opened it, and the dialog frame that control sat in, if any. */
const openedFrom = new WeakMap();

/* The control that has the focus when a dialog opens; a control that is still there but disabled counts when the focus
   has fallen to the page. */
function currentOpener() {
    const active = document.activeElement;
    if (active && active !== document.body) {
        return active;
    }
    return lastFocused && lastFocused.isConnected && lastFocused.disabled ? lastFocused : null;
}

/* A dialog opened from another dialog's control, which that dialog then removed (a prompt that closes as the next
   dialog opens), hands the focus on to the control that opened the removed dialog. */
function liveOpener(opener, parentFrame) {
    while (opener && !opener.isConnected && parentFrame) {
        const record = openedFrom.get(parentFrame);
        opener = record ? record.opener : null;
        parentFrame = record ? record.parentFrame : null;
    }
    return opener;
}

/* Give the focus back to the control that opened a dialog once the dialog is gone: the control may be disabled for a
   moment (its action is still running) or stay hidden behind another dialog, so a few looks are made while nothing else
   has taken the focus. */
function restoreFocus(opener, parentFrame, tries = RESTORE_TRIES) {
    opener = liveOpener(opener, parentFrame);
    if (!opener || !opener.isConnected || typeof opener.focus !== "function") {
        return;
    }
    const active = document.activeElement;
    if (active && active !== document.body && active !== opener) {
        return;
    }
    if (opener.disabled) {
        if (tries > 0) {
            setTimeout(() => restoreFocus(opener, parentFrame, tries - 1), RESTORE_EVERY);
        }
        return;
    }
    opener.focus();
}

const FOCUSABLE =
    "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export class Dialog extends Component {
    static props = {
        title: String,
        onClose: Function,
        large: { type: Boolean, optional: true },
        slots: Object,
    };
    static template = xml`
        <div class="viin-backdrop"/>
        <div class="modal viin-modal" tabindex="-1" role="dialog" aria-modal="true" t-att-aria-label="props.title" t-ref="frame">
            <div t-attf-class="modal-dialog modal-dialog-centered modal-dialog-scrollable {{ props.large ? 'modal-lg' : '' }}">
                <div class="modal-content">
                    <div class="modal-header">
                        <h2 class="modal-title" t-esc="props.title"/>
                        <button type="button" class="btn-close" t-att-aria-label="t('common.close')" t-on-click="props.onClose"/>
                    </div>
                    <div class="modal-body">
                        <t t-slot="default"/>
                    </div>
                    <div class="modal-footer" t-if="props.slots.footer">
                        <t t-slot="footer"/>
                    </div>
                </div>
            </div>
        </div>`;

    setup() {
        this.t = t;
        this.frame = useRef("frame");
        // The element that had focus when the dialog opened gets it back when the dialog goes.
        const opener = currentOpener();
        const parentFrame = opener ? opener.closest(".viin-modal") : null;
        useExternalListener(document, "keydown", (event) => {
            const frames = document.querySelectorAll(".viin-modal");
            if (frames[frames.length - 1] !== this.frame.el) {
                return;
            }
            if (event.key === "Escape") {
                this.props.onClose();
            } else if (event.key === "Tab") {
                this.keepFocusInside(event);
            }
        });
        // The dialog frame is still in the page here, so the look is made once it has been taken out.
        onWillUnmount(() => setTimeout(() => restoreFocus(opener, parentFrame), 0));
        onMounted(() => {
            openedFrom.set(this.frame.el, { opener, parentFrame });
            const field =
                this.frame.el &&
                this.frame.el.querySelector("input:not([type=hidden]), textarea, select");
            (field || this.frame.el).focus();
        });
    }
    /* Tab and Shift+Tab go round the visible controls of the dialog and never leave it. */
    keepFocusInside(event) {
        const frame = this.frame.el;
        const controls = [...frame.querySelectorAll(FOCUSABLE)].filter(
            (control) => control.getClientRects().length
        );
        if (!controls.length) {
            event.preventDefault();
            frame.focus();
            return;
        }
        const first = controls[0];
        const last = controls[controls.length - 1];
        const active = document.activeElement;
        if (!frame.contains(active) || active === frame) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
        }
    }
}
