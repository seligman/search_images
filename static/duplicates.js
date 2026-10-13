/* Duplicates page: stacks of pictures that look alike, the best of each,
   and what to do with the others.

   The stacks come from GET /api/admin/duplicates (duplicates.py): the
   search page's stacks, the largest first, a page at a time, each with its
   pictures best first and the reason for the best in words. Hiding goes
   through POST /api/admin/images/hide (nothing is deleted), "Not
   duplicates" through POST /api/admin/duplicates/not-duplicates. The last
   action can be undone from the notice it leaves.

   The address holds the choices: ?kind=exact|similar|same, ?min=3,
   ?page=2, and ?stack=<picture id> for the one stack that holds that
   picture (the search page links there); the library is the header's.
   kind=same ("Same picture, any date") lists pictures that look the same
   whatever their dates, which the search page does not stack together
   (duplicates.build_same_stacks: it needs the looks). Its pictures link to
   their stacks on the search page (?stack=<id>&from=same, which keeps the
   list's page and offers the way back).

   Built with DOM calls and textContent only, never innerHTML.
   DUPLICATES_FORMAT holds the parts that need no page (the tests load it
   under Node).
*/
var DUPLICATES_FORMAT = (function () {
    "use strict";

    var KINDS = ["all", "exact", "similar", "same"];
    var LABELS = {all: "All stacks", exact: "Identical files", similar: "Similar pictures",
                  same: "Same picture, any date"};
    var MINIMUMS = [2, 3, 5, 10];

    // The page's choices from the address (SHELL.getState()): anything
    // else than the values the page offers is the default.
    function choices(wanted) {
        wanted = wanted || {};
        var min = parseInt(wanted.min, 10);
        var page = parseInt(wanted.page, 10);
        var stack = parseInt(wanted.stack, 10);
        return {
            kind: KINDS.indexOf(wanted.kind) >= 0 ? wanted.kind : "all",
            min: MINIMUMS.indexOf(min) >= 0 ? min : 2,
            page: page > 0 ? page : 1,
            stack: stack > 0 ? stack : null,
            // (Where a stack was opened from: "same" offers the way back.)
            from: stack > 0 && wanted.from === "same" ? "same" : null
        };
    }

    // The address (SHELL.setState's changes) of a page's choices.
    function address(state) {
        return {
            kind: state.kind === "all" ? null : state.kind,
            min: state.min === 2 ? null : String(state.min),
            page: state.page > 1 ? String(state.page) : null,
            stack: state.stack ? String(state.stack) : null,
            from: state.stack && state.from ? state.from : null
        };
    }

    // The query string ("?lib=x&stack=7&from=same") of the search page's
    // stack that holds picture id, opened from "Same picture, any date"
    // whose choices are state, with the other parameters of query (the
    // address's query string) kept.
    function stackQuery(query, state, id) {
        var params = new URLSearchParams(query || "");
        var changes = address({kind: "all", min: state.min, page: state.page, stack: id,
                               from: "same"});
        Object.keys(changes).forEach(function (key) {
            if (changes[key] === null) {
                params.delete(key);
            } else {
                params.set(key, changes[key]);
            }
        });
        return "?" + params.toString();
    }

    // "14 May 2019, 14:23" from "2019-05-14T14:23:01"; the day alone when
    // there is no time (a date in a file name is stored as midnight).
    function dateText(text, source) {
        var match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(text || "");
        if (!match) {
            return "";
        }
        var date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        var day = date.toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"});
        var midnight = match[4] === "00" && match[5] === "00" && source !== "camera";
        return match[4] && !midnight ? day + ", " + match[4] + ":" + match[5] : day;
    }

    function sizeText(member) {
        if (!member.width || !member.height) {
            return "Size not known";
        }
        var pixels = member.width * member.height;
        var text = member.width + " \u00d7 " + member.height;
        return pixels >= 100000 ? text + " \u00b7 " + (pixels / 1e6).toFixed(1) + " MP" : text;
    }

    function plural(count, word) {
        return count + " " + word + (count === 1 ? "" : "s");
    }

    // How many other pictures of a stack are the same file as member
    // ("twin" names the first of them): they stay with it whatever is said.
    function sameFiles(stack, member) {
        var first = member.twin || member.id;
        return stack.members.filter(function (other) {
            return other.id !== member.id && (other.twin || other.id) === first;
        }).length;
    }

    // What "Not a duplicate" on member (called name) does, in words.
    function apartText(stack, member, name) {
        var copies = sameFiles(stack, member);
        var what = stack.kind === "same" ? "listed" : "stacked";
        return copies ? name + " and its " + plural(copies, "identical file") +
            " are no longer " + what + " with the others." : name + " is no longer " + what +
            " with the others.";
    }

    // What "Not duplicates" on a stack does, in words.
    // The owner's marks of a member in words: "\u2605 Favorite \u00b7 4 stars \u00b7 in 2 albums".
    function marksText(member) {
        var parts = [];
        if (member.favorite) {
            parts.push("\u2605 Favorite");
        }
        if (member.rating) {
            parts.push(plural(member.rating, "star"));
        }
        if (member.albums) {
            parts.push(member.albums === 1 ? "in an album" : "in " + member.albums + " albums");
        }
        return parts.join(" \u00b7 ");
    }

    // A stack's title: "3 identical files", "2 similar pictures", "4 pictures
    // that look the same".
    function stackTitle(stack) {
        if (stack.kind === "same") {
            return plural(stack.size, "picture") + " that look the same";
        }
        return stack.kind === "exact" ? plural(stack.size, "identical file")
            : plural(stack.size, "similar picture");
    }

    // "Taken 14 May 2019 to 2 Jun 2021" for a stack whose pictures were
    // taken on different days ("" when they were not, or have no dates).
    function spanText(stack) {
        var days = [];
        stack.members.forEach(function (member) {
            var match = /^(\d{4}-\d{2}-\d{2})/.exec(member.taken || "");
            if (match) {
                days.push(match[1]);
            }
        });
        days.sort();
        if (days.length < 2 || days[0] === days[days.length - 1]) {
            return "";
        }
        return "Taken " + dateText(days[0]) + " to " + dateText(days[days.length - 1]);
    }

    // What the search page does with member of a "Same picture, any date"
    // stack ("" when it shows it alone): the link to that stack.
    function stackedText(member) {
        return member.stacked > 1 ? "Its stack of " + member.stacked + " (search page)" : "";
    }

    // The pictures of a "Same picture, any date" stack that share a stack of
    // the search page: lists of two ids or more ("Not duplicates" leaves
    // them stacked).
    function togetherOf(stack) {
        var groups = {};
        var order = [];
        stack.members.forEach(function (member) {
            if (member.stack) {
                var key = String(member.stack);
                if (!groups[key]) {
                    groups[key] = [];
                    order.push(key);
                }
                groups[key].push(member.id);
            }
        });
        return order.map(function (key) {
            return groups[key];
        }).filter(function (ids) {
            return ids.length > 1;
        });
    }

    function notDuplicatesText(stack) {
        if (stack.kind === "same") {
            return "These pictures are no longer listed as the same picture; their stacks on " +
                "the search page stay as they are.";
        }
        return stack.contents < stack.size
            ? "These pictures are no longer stacked together, except identical files: those " +
                "always stay together."
            : "These " + stack.size + " pictures are no longer stacked together.";
    }

    // What undoing "Not duplicates" (or "Not a duplicate") on a stack did.
    function undoneText(stack) {
        return stack.kind === "same" ? "They are listed together again."
            : "They are stacked again.";
    }

    var api = {KINDS: KINDS, LABELS: LABELS, MINIMUMS: MINIMUMS, choices: choices,
               address: address, stackQuery: stackQuery, dateText: dateText, sizeText: sizeText,
               sameFiles: sameFiles, apartText: apartText, notDuplicatesText: notDuplicatesText,
               undoneText: undoneText, togetherOf: togetherOf, marksText: marksText,
               stackTitle: stackTitle, spanText: spanText, stackedText: stackedText};
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
    return api;
})();

(function () {
    "use strict";

    if (typeof window === "undefined" || !window.SHELL) {
        return;                 // (the tests, under Node)
    }
    var F = DUPLICATES_FORMAT;
    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;

    var state = F.choices({});
    var answer = null;          // the last GET /duplicates answer (null: it failed)
    var loadCount = 0;          // only the latest request is shown
    var working = false;        // an action, or the reload after it, is on its way

    function byId(id) {
        return document.getElementById(id);
    }

    /* ---------- Address ------------------------------------------------------------ */

    function readAddress() {
        state = F.choices(SHELL.getState());
    }

    function writeAddress() {
        SHELL.setState(F.address(state));
    }

    /* ---------- Words ------------------------------------------------------------- */

    function libraryName(id) {
        var name = "";
        ((window.SITE || {}).libraryList || []).forEach(function (item) {
            if (item.id === id) {
                name = item.name;
            }
        });
        return name;
    }

    var stackTitle = F.stackTitle;

    // What messages call a picture: its file name (the pictures of a stack
    // often share one title).
    function pictureName(member) {
        return member.name || member.title || "Picture " + member.id;
    }

    function filePath(member) {
        return member.folder ? member.folder + "/" + member.name : member.name;
    }

    /* ---------- Notices and undo -------------------------------------------------- */

    // One notice at a time: the result of the last action, with Undo
    // (undo() returns a promise of what to say once it is undone).
    function showNotice(kind, text, undo) {
        var area = byId("dup-notices");
        clear(area);
        var actions = [];
        if (undo) {
            var button = el("button", {type: "button", className: "button", text: "Undo"});
            button.addEventListener("click", function () {
                button.disabled = true;
                act(undo).then(function (done) {
                    showNotice("", done);
                }, function () {
                    button.disabled = false;
                });
            });
            actions.push(button);
        }
        var close = el("button", {type: "button", className: "button subtle", text: "Dismiss"});
        close.addEventListener("click", function () {
            clear(area);
        });
        actions.push(close);
        area.appendChild(SHELL.notice(kind, text, actions));
    }

    function setWorking(on) {
        working = on;
        document.body.classList.toggle("is-working", on);
        byId("keep-all").disabled = on;
    }

    // Run an action (a function returning a promise) unless another one is
    // on its way; then show the stacks as they are now. The page stays busy
    // until they are shown, so no button acts on stacks that are gone.
    function act(run) {
        if (working) {
            SHELL.toast("Just a moment: the last change is still being made.", "info", 4);
            return Promise.reject(new Error("busy"));
        }
        setWorking(true);
        return run().then(function (result) {
            return load().then(function () {
                setWorking(false);
                return result;
            });
        }, function (error) {
            // (Part of it may have been done: show the stacks as they are.)
            return load().then(function () {
                setWorking(false);
                throw error;
            });
        });
    }

    function setHidden(ids, hidden) {
        var parts = [];
        for (var start = 0; start < ids.length; start += 10000) {
            parts.push(ids.slice(start, start + 10000));
        }
        return parts.reduce(function (done, part) {
            return done.then(function (count) {
                return SHELL.admin("/images/hide", {ids: part, hidden: hidden}).then(function (reply) {
                    return count + (reply.changed || 0);
                });
            });
        }, Promise.resolve(0));
    }

    function hide(ids, describe) {
        return act(function () {
            return setHidden(ids, true);
        }).then(function (count) {
            showNotice("ok", describe(count), function () {
                return setHidden(ids, false).then(function (shown) {
                    return fmt.plural(shown, "picture") + " shown again.";
                });
            });
        }, function () {
            // (The error was shown by SHELL.admin.)
        });
    }

    // "Not duplicates" on the pictures ids of stack (apart: only that one
    // from the others). In "Same picture, any date", pictures that share a
    // stack of the search page stay stacked (together).
    function keepApart(stack, ids, apart, describe) {
        var together = stack.kind === "same" ? F.togetherOf(stack) : [];
        function request(undo) {
            var body = {ids: ids};
            if (apart) {
                body.apart = apart;
            }
            if (together.length) {
                body.together = together;
            }
            if (undo) {
                body.undo = true;
            }
            return body;
        }
        return act(function () {
            return SHELL.admin("/duplicates/not-duplicates", request(false));
        }).then(function () {
            showNotice("ok", describe, function () {
                return SHELL.admin("/duplicates/not-duplicates", request(true)).then(function () {
                    return F.undoneText(stack);
                });
            });
        }, function () {
            // (The error was shown by SHELL.admin.)
        });
    }

    function memberIds(stack) {
        return stack.members.map(function (member) {
            return member.id;
        });
    }

    function othersOf(stack, keep) {
        return memberIds(stack).filter(function (id) {
            return id !== keep;
        });
    }

    // A picture the owner marked: a favorite, rated, or in an album.
    function isMarked(member) {
        return Boolean(member.favorite || member.rating || member.albums);
    }

    function keepOnly(stack, member) {
        var ids = othersOf(stack, member.id);
        return hide(ids, function (count) {
            return "Kept " + pictureName(member) + " and hid " + fmt.plural(count, "other picture") +
                " of its stack.";
        });
    }

    function keepAllOnPage() {
        if (working || !answer) {
            return;             // (The button is off meanwhile.)
        }
        var shown = answer;
        var stacks = shown.stacks || [];
        var ids = [];
        var marked = 0;
        stacks.forEach(function (stack) {
            // (Pictures the owner marked stay: hide them one by one if meant.)
            stack.members.forEach(function (member) {
                if (member.id === stack.best) {
                    return;
                }
                if (isMarked(member)) {
                    marked += 1;
                } else {
                    ids.push(member.id);
                }
            });
        });
        if (!ids.length) {
            if (marked) {
                showNotice("warn", "Every other picture of these stacks is one you marked (a " +
                           "favorite, rated, or in an album), so none was hidden. Hide them " +
                           "one by one if you want.");
            }
            return;
        }
        SHELL.confirm({
            title: "Keep the best of " + fmt.plural(stacks.length, "stack") + "?",
            message: [
                el("p", {text: "This hides " + fmt.plural(ids.length, "picture") + " and keeps the " +
                         "best one of each of the " + fmt.plural(stacks.length, "stack") +
                         " on this page." + (marked ? " " + fmt.plural(marked, "picture") +
                         " you marked (a favorite, rated, or in an album) stay too." : "")}),
                el("p", {text: "Hidden pictures are left out of searches and exports; nothing is " +
                         "deleted. Undo it right after, or unhide them later with \u201cShow " +
                         "hidden\u201d on the search page."})
            ],
            confirmText: "Hide " + fmt.plural(ids.length, "picture")
        }).then(function (yes) {
            if (!yes) {
                return;
            }
            if (answer !== shown) {
                // The stacks were loaded again meanwhile: what was counted
                // may be gone or different.
                showNotice("warn", "The stacks changed meanwhile, so nothing was hidden. " +
                           "Look them over and try again.");
                return;
            }
            hide(ids, function (count) {
                return "Kept the best of " + fmt.plural(stacks.length, "stack") + " and hid " +
                    fmt.plural(count, "picture") + ".";
            });
        });
    }

    /* ---------- Stacks ------------------------------------------------------------- */

    function fact(label, value, className) {
        if (!value) {
            return null;
        }
        return el("div", {className: "dup-fact" + (className ? " " + className : "")}, [
            el("dt", {text: label}),
            el("dd", {text: value})
        ]);
    }

    // The describe step's scores (1 to 5).
    function scores(member) {
        var parts = [];
        if (member.sharpness) {
            parts.push("sharpness " + member.sharpness + "/5");
        }
        if (member.aesthetic) {
            parts.push("looks " + member.aesthetic + "/5");
        }
        return parts.join(" \u00b7 ");
    }

    function memberCard(stack, member) {
        var best = member.id === stack.best;
        var name = pictureName(member);
        var image = el("img", {alt: member.title || name, src: SITE.thumbUrl(member), loading: "lazy",
                               draggable: false});
        image.decoding = "async";
        image.addEventListener("error", function () {
            image.parentNode.classList.add("no-thumb");
        });
        var thumb = el("a", {className: "dup-thumb", href: SITE.detailUrl(member.id),
                             title: "Open " + name}, [image]);
        if (member.p === "360") {
            thumb.appendChild(el("span", {className: "dup-tag", text: "360\u00b0"}));
        }
        var taken = F.dateText(member.taken, member.date_source);
        var notes = [];
        if (best) {
            notes.push(el("span", {className: "dup-badge", text: "Best"}));
        }
        if (member.twin) {
            notes.push(el("span", {className: "dup-twin",
                                   text: member.twin === stack.best ? "Same file as the best"
                                       : "Same file as another one here"}));
        }
        if (isMarked(member)) {
            notes.push(el("span", {className: "dup-marks", text: F.marksText(member)}));
        }
        if (stack.kind === "same" && F.stackedText(member)) {
            notes.push(el("a", {className: "dup-stacked",
                                href: F.stackQuery(window.location.search, state, member.id),
                                text: F.stackedText(member),
                                title: "Show the stack it is in on the search page",
                                on: {click: function (event) {
                                    // (A new tab or window opens the address.)
                                    if (event.button || event.ctrlKey || event.metaKey ||
                                            event.shiftKey || event.altKey) {
                                        return;
                                    }
                                    event.preventDefault();
                                    showStackOf(member.id);
                                }}}));
        }
        var facts = el("dl", {className: "dup-facts"}, [
            fact("Size", F.sizeText(member)),
            fact("File", [member.bytes ? fmt.bytes(member.bytes) : "", member.format]
                .filter(Boolean).join(" \u00b7 ")),
            fact("Taken", taken ? taken + (member.date_source ? " (" + member.date_source + ")" : "")
                 : ""),
            fact("Quality", scores(member)),
            fact("Camera", member.camera),
            // (A stack of pictures in more than one library.)
            stack.libraries ? fact("Library", libraryName(member.library) || member.library) : null,
            fact("File name", filePath(member), "dup-path")
        ]);
        var card = el("li", {className: "dup-member" + (best ? " is-best" : "")}, [
            thumb,
            notes.length ? el("div", {className: "dup-notes"}, notes) : null,
            member.title ? el("p", {className: "dup-title", text: member.title}) : null,
            facts,
            member.copy && member.copy.length
                ? el("p", {className: "dup-copy", text: "Looks like a copy: " + member.copy.join("; ") + "."})
                : null
        ]);
        var actions = el("div", {className: "dup-member-actions"});
        if (!best) {
            actions.appendChild(el("button", {
                type: "button", className: "button small", text: "Keep only this",
                attrs: {"aria-label": "Keep only " + name + " and hide the others"},
                on: {click: function () {
                    keepOnly(stack, member);
                }}
            }));
        }
        actions.appendChild(el("button", {
            type: "button", className: "button small", text: "Hide",
            attrs: {"aria-label": "Hide " + name},
            on: {click: function () {
                hide([member.id], function (count) {
                    return count ? "Hid " + name + "." : name + " was hidden already.";
                });
            }}
        }));
        if (stack.contents > 1 && member.can_apart !== false) {
            actions.appendChild(el("button", {
                type: "button", className: "button small subtle", text: "Not a duplicate",
                attrs: {"aria-label": name + " is not a duplicate of the others"},
                on: {click: function () {
                    keepApart(stack, memberIds(stack), member.id, F.apartText(stack, member, name));
                }}
            }));
        }
        card.appendChild(actions);
        return card;
    }

    function stackCard(stack, showLibrary) {
        var titleId = "dup-stack-" + stack.id;
        var head = [el("h2", {id: titleId, text: stackTitle(stack)})];
        if (stack.libraries) {
            // ("Same picture, any date": pictures kept in more than one library.)
            var names = stack.libraries.map(function (id) {
                return libraryName(id) || id;
            });
            head.push(el("span", {className: "dup-library", text: names.join(" \u00b7 ")}));
        } else if (showLibrary && libraryName(stack.library)) {
            head.push(el("span", {className: "dup-library", text: libraryName(stack.library)}));
        }
        if (stack.kind === "same" && F.spanText(stack)) {
            head.push(el("span", {className: "dup-span", text: F.spanText(stack)}));
        }
        var actions = el("div", {className: "dup-stack-actions"}, [
            el("button", {type: "button", className: "button primary", text: "Keep best, hide the others",
                          on: {click: function () {
                              var best = stack.members[0];
                              keepOnly(stack, best);
                          }}})
        ]);
        var limit = (answer && answer.not_duplicates_limit) || 100;
        // (Nothing to keep apart: identical files, or pictures of one stack
        // of the search page, in "Same picture, any date".)
        var separable = stack.contents > 1 && stack.can_apart !== false;
        if (separable && stack.size <= limit) {
            actions.appendChild(el("button", {
                type: "button", className: "button", text: "Not duplicates",
                title: "Stop stacking these pictures (identical files stay together)",
                on: {click: function () {
                    keepApart(stack, memberIds(stack), null, F.notDuplicatesText(stack));
                }}
            }));
        } else if (separable) {
            actions.appendChild(el("span", {
                className: "dup-limit",
                text: "Too many pictures to call them all not duplicates at once (at most " +
                    fmt.number(limit) + "); use \u201cNot a duplicate\u201d on single pictures."
            }));
        }
        return el("section", {className: "dup-stack card", attrs: {"aria-labelledby": titleId}}, [
            el("div", {className: "dup-stack-head"}, [
                el("div", {className: "dup-stack-title"}, head),
                actions
            ]),
            el("p", {className: "dup-reason"}, [
                el("strong", {text: "Best: "}),
                stack.reason
            ]),
            el("ol", {className: "dup-members", attrs: {"aria-label": stackTitle(stack)}},
               stack.members.map(function (member) {
                   return memberCard(stack, member);
               }))
        ]);
    }

    /* ---------- The page ----------------------------------------------------------- */

    function renderTools() {
        var counts = (answer && answer.counts) || {};
        Array.prototype.forEach.call(document.querySelectorAll("#dup-tools [data-kind]"), function (button) {
            var kind = button.getAttribute("data-kind");
            button.setAttribute("aria-pressed", String(kind === state.kind && !state.stack));
            button.textContent = F.LABELS[kind] + (counts[kind] !== undefined ? " (" +
                fmt.number(counts[kind]) + ")" : "");
        });
        byId("dup-min").value = String(state.min);
    }

    function emptyMessage() {
        var where = SHELL.libraryName() ? " in " + SHELL.libraryName() : "";
        if (state.stack) {
            return [el("p", {text: "This stack is gone: its pictures were hidden, kept apart or " +
                             "changed since."}),
                    el("p", {}, [state.from === "same"
                        ? el("a", {href: "#", text: "Back to Same picture, any date",
                                   on: {click: backToSame}})
                        : el("a", {href: "#", text: "Show all stacks", on: {click: showAll}})])];
        }
        if (state.kind === "same") {
            if (answer && !answer.looked) {
                return [el("p", {text: "This needs the looks of the pictures" + where + "."}),
                        el("p", {className: "muted", text: "Looks are made by an image model on " +
                                 "this computer: set it up as SETTINGS.md describes, then use " +
                                 "\u201cUpdate looks\u201d on the Manage page."})];
            }
            return [el("p", {text: "No pictures" + where + " that look the same apart from " +
                             "the stacks on the search page."}),
                    el("p", {className: "muted", text: "This lists pictures that look the same " +
                             "whatever their dates (a copy saved again later or kept in another " +
                             "library, a picture without a date) when the search page does not " +
                             "stack them together. With all libraries shown, it compares them " +
                             "with each other too."})];
        }
        if (answer && answer.counts && answer.counts.all) {
            return [el("p", {text: "No stacks of this kind" + where + "."})];
        }
        return [el("p", {text: "No duplicates" + where + "."}),
                el("p", {className: "muted", text: "Pictures are stacked when they are identical files, " +
                         "or nearly the same shot taken at about the same time. Scan your libraries " +
                         "on the Manage page to find new ones."})];
    }

    // The search page's stack that holds picture id, from "Same picture, any
    // date" (a new history entry: Back returns to the list).
    function showStackOf(id) {
        var query = F.stackQuery(window.location.search, state, id);
        window.history.pushState(null, "", window.location.pathname + query + window.location.hash);
        readAddress();
        load().then(function () {
            window.scrollTo(0, 0);
        });
    }

    function showAll(event) {
        if (event) {
            event.preventDefault();
        }
        state.stack = null;
        state.from = null;
        state.page = 1;
        writeAddress();
        load();
    }

    // Back to the "Same picture, any date" page a stack was opened from.
    function backToSame(event) {
        if (event) {
            event.preventDefault();
        }
        state.kind = "same";
        state.stack = null;
        state.from = null;
        writeAddress();
        load().then(function () {
            window.scrollTo(0, 0);
        });
    }

    function renderPages() {
        var nav = byId("dup-pages");
        clear(nav);
        if (!answer || state.stack || answer.pages < 2) {
            nav.hidden = true;
            return;
        }
        function go(page) {
            return function () {
                state.page = page;
                writeAddress();
                load().then(function () {
                    window.scrollTo(0, 0);
                });
            };
        }
        nav.appendChild(el("button", {type: "button", className: "button", text: "Previous",
                                      disabled: answer.page <= 1, on: {click: go(answer.page - 1)}}));
        nav.appendChild(el("span", {className: "dup-page-of",
                                    text: "Page " + fmt.number(answer.page) + " of " +
                                        fmt.number(answer.pages)}));
        nav.appendChild(el("button", {type: "button", className: "button", text: "Next",
                                      disabled: answer.page >= answer.pages,
                                      on: {click: go(answer.page + 1)}}));
        nav.hidden = false;
    }

    function render() {
        renderTools();
        var list = byId("dup-list");
        clear(list);
        var summary = byId("dup-summary");
        clear(summary);
        var stacks = answer.stacks || [];
        if (state.stack) {
            summary.appendChild(document.createTextNode(state.kind === "same"
                ? "One stack of pictures that look the same. " : "One stack, from the search page. "));
            if (state.from === "same") {
                summary.appendChild(el("a", {href: "#", text: "Back to Same picture, any date",
                                             on: {click: backToSame}}));
                summary.appendChild(document.createTextNode(" \u00b7 "));
            }
            summary.appendChild(el("a", {href: "#", text: "Show all stacks", on: {click: showAll}}));
        } else if (answer.total) {
            summary.textContent = fmt.plural(answer.total, "stack") + " \u00b7 " +
                fmt.plural(answer.pictures, "picture") + " \u00b7 keeping the best of each would hide " +
                fmt.number(answer.extra);
        }
        if (!stacks.length) {
            list.appendChild(el("div", {className: "dup-empty card"}, emptyMessage()));
        }
        var showLibrary = !SHELL.library() && ((window.SITE || {}).libraryList || []).length > 1;
        stacks.forEach(function (stack) {
            list.appendChild(stackCard(stack, showLibrary));
        });
        // (One stack has its own button.)
        byId("keep-all").hidden = stacks.length < 2;
        renderPages();
    }

    function load() {
        loadCount += 1;
        var mine = loadCount;
        var params = new URLSearchParams();
        if (SHELL.library()) {
            params.set("lib", SHELL.library());
        }
        if (state.stack) {
            params.set("stack", String(state.stack));
            if (state.kind === "same") {
                params.set("kind", "same");
            }
        } else {
            params.set("kind", state.kind);
            params.set("min", String(state.min));
            params.set("page", String(state.page));
        }
        return SHELL.admin("/duplicates?" + params.toString(), undefined, {quiet: true}).then(function (data) {
            if (mine !== loadCount) {
                return;
            }
            answer = data;
            if (!state.stack && data.page !== state.page) {
                state.page = data.page;
                writeAddress();
            }
            render();
        }, function (error) {
            if (mine !== loadCount) {
                return;
            }
            // Nothing on the page is current any more: no button may act on it.
            answer = null;
            renderTools();
            byId("keep-all").hidden = true;
            clear(byId("dup-summary"));
            renderPages();
            var list = byId("dup-list");
            clear(list);
            var again = el("button", {type: "button", className: "button", text: "Try again",
                                      on: {click: function () {
                                          load();
                                      }}});
            list.appendChild(SHELL.notice("error", error.message || "The stacks could not be loaded.",
                                          [again]));
        });
    }

    function start() {
        readAddress();
        writeAddress();
        Array.prototype.forEach.call(document.querySelectorAll("#dup-tools [data-kind]"), function (button) {
            button.addEventListener("click", function () {
                state.kind = button.getAttribute("data-kind");
                state.stack = null;
                state.from = null;
                state.page = 1;
                writeAddress();
                load();
            });
        });
        byId("dup-min").addEventListener("change", function () {
            state.min = parseInt(byId("dup-min").value, 10) || 2;
            state.stack = null;
            state.page = 1;
            writeAddress();
            load();
        });
        byId("keep-all").addEventListener("click", keepAllOnPage);
        // (Back and Forward after showStackOf.)
        window.addEventListener("popstate", function () {
            readAddress();
            load();
        });
        SHELL.onLibraryChange(function () {
            state.page = 1;
            state.stack = null;
            state.from = null;
            writeAddress();
            load();
        });
        renderTools();
        load();
    }

    start();
})();
