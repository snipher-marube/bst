/**
 * Jodit WYSIWYG editor for newsletter campaign HTML content.
 * Initialized on any textarea[data-editor="email-html"].
 * Provides full visual editing + raw HTML source toggle.
 */
(function () {
    "use strict";

    function initEditors() {
        var targets = document.querySelectorAll('textarea[data-editor="email-html"]');
        targets.forEach(function (textarea) {
            if (textarea._joditInitialized) return;
            textarea._joditInitialized = true;

            var editor = Jodit.make(textarea, {
                height: 550,
                theme: "default",
                language: "en",
                useSplitMode: false,
                toolbarSticky: true,
                toolbarStickyOffset: 45,   // offset below Django admin header bar

                // Keep inline styles — critical for email HTML
                cleanHTML: {
                    fillEmptyParagraph: false,
                    replaceNBSP: false,
                    removeEmptyElements: false,
                },
                style: {
                    "font-family": "Arial, sans-serif",
                    "font-size": "16px",
                },

                // Email-safe toolbar (no iframes, no JS)
                buttons: [
                    "source",         // toggle raw HTML view
                    "|",
                    "bold", "italic", "underline", "strikethrough",
                    "|",
                    "fontsize", "paragraph",
                    "|",
                    "brush",          // text colour
                    "|",
                    "align",
                    "|",
                    "ul", "ol",
                    "|",
                    "outdent", "indent",
                    "|",
                    "link", "image",
                    "|",
                    "hr", "table",
                    "|",
                    "copyformat",
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
                    "link", "image", "|",
                    "undo", "redo", "|", "fullsize",
                ],
                buttonsSM: ["source", "bold", "italic", "link", "image", "fullsize"],

                // Allow all HTML attributes so email tables / inline styles survive
                allowResizeTags: ["img", "table"],
                allowedTags: false,   // false = allow everything
                disablePlugins: "xpath,search",

                // Prevent Jodit from stripping inline styles
                processPasteHTML: false,
                defaultActionOnPaste: "insert_only_text",

                events: {
                    // Make the toolbar hint visible
                    afterInit: function (joditInstance) {
                        var hint = document.getElementById("editor-hint-" + textarea.id);
                        if (hint) hint.style.display = "block";
                    },
                },
            });

            // Sync back to original textarea on every change so Django form sees the value
            editor.events.on("change", function (newValue) {
                textarea.value = newValue;
            });
        });
    }

    // Django admin loads scripts late — wait for DOMContentLoaded
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initEditors);
    } else {
        // Already loaded (e.g. deferred script)
        initEditors();
    }
})();
