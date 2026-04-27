/**
 * Jodit WYSIWYG editor — newsletter campaign content field.
 *
 * Initialised on any <textarea data-editor="email-html">.
 * Key behaviours:
 *   - Preserves all inline styles (required for email clients).
 *   - Allows raw HTML source editing via the </> button.
 *   - Pastes HTML as-is (not stripped to plain text).
 *   - Syncs value back to the hidden textarea on every change so
 *     Django's form submission always receives the latest content.
 */
(function () {
    "use strict";

    function initEditors() {
        document.querySelectorAll('textarea[data-editor="email-html"]').forEach(function (textarea) {
            if (textarea._joditReady) { return; }
            textarea._joditReady = true;

            var editor = Jodit.make(textarea, {
                /* ── Layout ── */
                height: 560,
                minHeight: 300,
                theme: "default",
                language: "en",
                toolbarSticky: true,
                toolbarStickyOffset: 46,   // clears the Django admin top bar

                /* ── Toolbar: email-relevant controls only ── */
                buttons: [
                    "source",                             // ← raw HTML toggle (most important)
                    "|",
                    "bold", "italic", "underline", "strikethrough",
                    "|",
                    "paragraph", "fontsize",
                    "|",
                    "brush",                              // text / background colour
                    "|",
                    "align",
                    "|",
                    "ul", "ol",
                    "|",
                    "link", "image",
                    "|",
                    "table", "hr",
                    "|",
                    "undo", "redo",
                    "|",
                    "fullsize",
                ],
                buttonsMD: [
                    "source", "|",
                    "bold", "italic", "underline", "|",
                    "brush", "paragraph", "|",
                    "align", "ul", "ol", "|",
                    "link", "image", "table", "|",
                    "undo", "redo", "fullsize",
                ],
                buttonsSM: ["source", "bold", "italic", "link", "fullsize"],

                /* ── HTML preservation ─────────────────────────────────────
                 *  Email content relies heavily on inline styles and table
                 *  layouts.  We disable every transformation that could
                 *  silently strip or rewrite the markup.
                 * ─────────────────────────────────────────────────────── */
                cleanHTML: {
                    fillEmptyParagraph: false,
                    replaceNBSP: false,
                    removeEmptyElements: false,
                    denyTags: false,        // allow all tags (tables, td, tr, …)
                },
                allowedTags: false,         // false = allow everything
                allowResizeTags: ["img", "table"],

                /* ── Paste: keep HTML formatting, never strip to plain text ── */
                askBeforePasteHTML: false,
                askBeforePasteFromWord: false,
                defaultActionOnPaste: "insert_as_html",
                processPasteHTML: false,
                processPasteFromWord: false,

                /* ── Disable plugins that can modify or break email HTML ── */
                disablePlugins: "xpath,search,powered-by-jodit",

                /* ── Editor body default font (visual only, not added to output) ── */
                editorCssClass: "nl-editor-body",
                style: {
                    "font-family": "Arial, Helvetica, sans-serif",
                    "font-size":   "15px",
                    "color":       "#333333",
                    "line-height": "1.6",
                },
            });

            /* Sync Jodit → textarea on every keystroke / toolbar action.
             * This ensures Django's POST always contains the latest content
             * even if the user submits without leaving the editor.            */
            editor.events.on("change", function (html) {
                textarea.value = html;
            });

            /* Also sync on form submit as a safety net. */
            var form = textarea.closest("form");
            if (form) {
                form.addEventListener("submit", function () {
                    textarea.value = editor.value;
                });
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initEditors);
    } else {
        initEditors();
    }
}());
