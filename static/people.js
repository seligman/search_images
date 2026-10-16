/* The People page (/people): the people the faces in the pictures show
   (people.py finds the faces, people_groups.py groups them), each with a
   face that stands for it, its name (or "Person 12" while it has none) and
   how many pictures it is in; a person opens its pictures in the search
   page (person:"Name").

   The admin also names people (Name, or Rename), opens one (?person=<id>)
   to see all its faces and put them right -- select faces (a click, Space
   or Enter on a face; Select all) and say "Not <name>" (they leave it for
   good), "Move to..." another person or a new one (a split), "Not a face",
   or "Use as cover" -- merges it into another person ("Merge into...", or
   the "Maybe the same person" suggestions: people whose faces look much
   like its own), hides it (strangers: not listed, not offered in searches),
   and says "None are faces" for a group of things. "Other faces"
   (?view=other) lists the faces in no person, best first, to put in a
   person or mark as not faces; "Not faces" (?view=not_face) those marked,
   to take the mark back. "Group faces again" (while People is on) queues
   the job that groups the faces anew (what the admin said is kept). The
   faces are pictures the server cuts from the previews (/face/<id>), or
   keeps (faces found in full size, and in the frames of videos). A face of
   a video says the moment it was seen at: its link ("0:12" after a play
   sign) opens the video's page there (detail?id=...&at=seconds); a face of
   a picture's ("Picture") opens its page with the face marked
   (detail?id=...&face=<id>).

   In the person picker (Move to..., Add to a person..., Merge into...)
   Enter chooses the person whose name is the one typed, else the new
   person of that name (when one can be made), else the only person whose
   name has the text in it; with several, nothing (click one).

   "Who's this?" (?view=queue, the admin's): the faces of nobody and the
   unnamed people the app is sure about (GET /api/admin/people/queue), one
   at a time, each asked about again right before it shows. Enter says yes,
   N "Not <name>", S or the right arrow skips it for now (this browser
   remembers it), X "not a face" (a face), U or Ctrl+Z undoes the last
   answer (one after another, also after leaving Who's this? or a reload:
   this tab keeps them, and the list offers "Undo last answer"), Esc
   leaves; each is a button too (on a phone, and on a short screen, a bar
   at the foot of the screen, the same for every item). Nothing is decided
   -- nor left -- without a key or a click, and not in the first moment an
   item shows (DECIDE_AFTER: a second press meant for the one before).

   Others (visitors, only when the server's "people" setting allows it)
   see the named people that are not hidden, and open their pictures.

   Pages are built with DOM calls and textContent only (never innerHTML).
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var adminOn = Boolean(SITE.capabilities.admin) && !SITE.capabilities.static;
    // While a grouping job runs: how often (ms) and how long (tries) the page
    // asks whether it has finished.
    var GROUP_POLL = 3000;
    var GROUP_TRIES = 200;
    var HIDDEN_KEY = "searchImages.peopleHidden";
    // "Who's this?": how often (ms) and how long (tries) the page asks again
    // while the server works the queue out; the items skipped "for now",
    // remembered by this browser (at most SKIPS_MAX) so they do not come back;
    // and how long (ms) an item shows before a key or a click decides it.
    var QUEUE_POLL = 1000;
    var QUEUE_TRIES = 120;
    var SKIPS_KEY = "searchImages.peopleQueueSkips";
    var SKIPS_MAX = 2000;
    var DECIDE_AFTER = 400;
    // The answers Undo can still take back (the server's tokens), kept by
    // this tab (sessionStorage, of this face data) so that Undo goes on after
    // leaving Who's this? or a reload: at most UNDO_KEEP, as many as the
    // server keeps (admin.UNDO_STEPS).
    var UNDO_KEY = "searchImages.peopleQueueUndo";
    var UNDO_KEEP = 20;
    // How long (ms) the list waits for "Who's this?" when the page opens, so
    // that the two show together (drawn after the list, the entry would push
    // it down); a queue still being worked out then shows when it comes.
    var QUEUE_HOLD = 400;

    function byId(id) {
        return document.getElementById(id);
    }

    var people = [];            // /api/people's people (the admin's: hidden ones too)
    var status = null;          // /api/admin/people (the admin's)
    var where = {view: "list"}; // what is shown: {view: "list" | "person" | "other" | "not_face", id}
    var shown = null;           // the person shown: /api/admin/people/<id>
    var faces = [];             // the faces shown (person or other faces): [{id, image, ...}]
    var selected = {};          // face id -> true
    var findText = "";
    var showHidden = adminOn && SHELL.storageGet(HIDDEN_KEY) === "1";
    var otherOffset = 0;
    var pollTimer = 0;
    var queue = null;           // /api/admin/people/queue (the admin's)
    var queueLoading = null;    // (the request for it on its way)
    var walk = null;            // the queue being walked (see startWalk)

    /* ---------- Small helpers ------------------------------------------------ */

    function personById(id) {
        for (var k = 0; k < people.length; k++) {
            if (people[k].id === id) {
                return people[k];
            }
        }
        return null;
    }

    // The person: term that names a person (by its id when its name cannot).
    function personTerm(person) {
        return SearchCore.personTerm(people, person);
    }

    function searchUrl(person) {
        return SITE.searchUrl({q: personTerm(person)});
    }

    function personUrl(id) {
        return "/people?person=" + encodeURIComponent(id);
    }

    function faceImage(id, alt) {
        var url = id !== null && id !== undefined ? SITE.faceUrl(id) : null;
        if (!url) {
            return el("span", {className: "person-pick-new", text: "?",
                               attrs: {"aria-hidden": "true"}});
        }
        return el("img", {src: url, alt: alt || "", loading: "lazy", decoding: "async"});
    }

    function notice(kind, content, actions) {
        var box = byId("people-notices");
        clear(box);
        if (content) {
            box.appendChild(SHELL.notice(kind, content, actions));
        }
    }

    // People is on (faces are found, and can be grouped again).
    function peopleOn() {
        return Boolean(status && status.people && status.people.enabled);
    }

    function picturesText(person) {
        var count = Array.isArray(person.ids) ? person.ids.length : 0;
        var videos = typeof person.videos === "number" ? person.videos : 0;
        return fmt.plural(count, "picture") + (videos ? " (" + fmt.plural(videos, "video") +
                                               ")" : "");
    }

    // 0:07, 1:02:03 (a moment of a video: whole seconds rounded down, as the
    // viewer's moments, VIEWER.format.momentText, say it too).
    function momentText(seconds) {
        var total = Math.max(0, Math.floor(seconds));
        var hours = Math.floor(total / 3600);
        var minutes = Math.floor(total / 60) % 60;
        var rest = total % 60;
        var two = function (value) {
            return (value < 10 ? "0" : "") + value;
        };
        return (hours ? hours + ":" + two(minutes) : String(minutes)) + ":" + two(rest);
    }

    // The page of the picture a face is in: a video's at the face's moment, a
    // picture's with the face marked (face=: a face of a panorama is a few pixels
    // across in it, see viewer.js faceBoxesFor).
    function faceUrl(face) {
        var url = SITE.detailUrl(face.image);
        var join = url.indexOf("?") >= 0 ? "&" : "?";
        if (typeof face.at === "number" && isFinite(face.at)) {
            return url + join + "at=" + encodeURIComponent(String(Math.round(face.at * 10) / 10));
        }
        return url + join + "face=" + encodeURIComponent(String(face.id));
    }

    function sameText(text) {
        return SearchCore.normalize(String(text || "")).replace(/\s+/g, " ")
            .replace(/^\s+|\s+$/g, "");
    }

    /* ---------- Loading -------------------------------------------------------- */

    function loadPeople() {
        return new Promise(function (resolve) {
            SITE.loadPeople(function (answer) {
                people = answer && Array.isArray(answer.people) ? answer.people : [];
                resolve(answer);
            }, adminOn);
        });
    }

    function loadStatus() {
        if (!adminOn) {
            return Promise.resolve(null);
        }
        return SHELL.admin("/people", undefined, {quiet: true}).then(function (answer) {
            status = answer;
            return answer;
        }, function () {
            status = null;
            return null;
        });
    }

    // Everything again, then what is shown.
    function reload() {
        return Promise.all([loadPeople(), loadStatus()]).then(function () {
            return show(where, false);
        });
    }

    /* ---------- Where we are (the address) ------------------------------------ */

    function wanted() {
        var params = new URLSearchParams(window.location.search);
        var id = params.get("person");
        if (adminOn && id && /^\d{1,18}$/.test(id)) {
            return {view: "person", id: Number(id)};
        }
        var view = params.get("view");
        if (adminOn && (view === "other" || view === "not_face" || view === "queue")) {
            return {view: view};
        }
        return {view: "list"};
    }

    function addressOf(place) {
        if (place.view === "person") {
            return personUrl(place.id);
        }
        if (place.view === "other" || place.view === "not_face" || place.view === "queue") {
            return "/people?view=" + place.view;
        }
        return "/people";
    }

    function go(place, push) {
        if (push) {
            window.history.pushState(null, "", addressOf(place));
        }
        selected = {};
        otherOffset = 0;
        window.scrollTo(0, 0);
        return show(place, true);
    }

    function link(text, place, className) {
        return el("a", {className: className || "", href: addressOf(place), text: text,
                        on: {click: function (event) {
                            if (event.button === 0 && !event.ctrlKey && !event.metaKey &&
                                    !event.shiftKey) {
                                event.preventDefault();
                                go(place, true);
                            }
                        }}});
    }

    // (queued: the queue was just asked for, see start.)
    function show(place, fresh, queued) {
        var fromQueue = where.view === "queue";
        where = place;
        // (Who's this? on a phone: more room for the faces; see people.css.)
        document.body.classList.toggle("people-queue-view", place.view === "queue");
        if (place.view !== "queue") {
            progressLine(null);
            say("");
            var left = walk;
            walk = null;
            if (left && left.history.length) {
                // (Answered: the people and their counts again, and the queue
                // without what was decided until it is asked for again.)
                dropDecided(left);
                return Promise.all([loadPeople(), loadStatus()]).then(function () {
                    if (where !== place) {
                        return null;
                    }
                    where = {view: "queue"};
                    return show(place, fresh);
                });
            }
        }
        if (place.view === "person") {
            return showPerson(place.id, fresh);
        }
        if (place.view === "other" || place.view === "not_face") {
            return showOther(place.view, fresh);
        }
        if (place.view === "queue") {
            return showQueue();
        }
        renderList();
        if (adminOn && !queued) {
            refreshQueueEntry();    // (the queue as the people are now: its count)
        }
        if (fromQueue) {
            focusEntry();           // (back from Who's this?: the keyboard is here)
        }
        return Promise.resolve();
    }

    /* ---------- The list of people -------------------------------------------- */

    function statusText() {
        var groups = status && status.groups;
        if (!groups) {
            return "";
        }
        var text = fmt.plural(groups.faces, "face") + " found; " + fmt.number(groups.grouped) +
            " of them in " + fmt.plural(groups.people, "person", "people") +
            " (" + fmt.number(groups.named) + " named" +
            (groups.hidden ? ", " + fmt.number(groups.hidden) + " hidden" : "") + ").";
        if (groups.grouped_at) {
            text += " Grouped " + fmt.relative(groups.grouped_at) + ".";
        }
        return text;
    }

    function renderHead() {
        var actions = byId("people-actions");
        clear(actions);
        if (adminOn && peopleOn()) {
            actions.appendChild(el("button", {type: "button", className: "button",
                                              text: "Group faces again",
                                              title: "Group every face into people anew (what " +
                                                  "you said about them is kept)",
                                              on: {click: groupAgain}}));
        }
        byId("people-lead").textContent = adminOn
            ? "The people in your pictures, found by their faces on this computer. Name them, " +
              "and open one to put its faces right. Nothing about faces or names leaves this " +
              "computer, unless an export's People's names option publishes the names."
            : "The people in these pictures. Open one to see the pictures they are in.";
    }

    function dueNotice() {
        var groups = status && status.groups;
        var finding = status && status.people;
        if (finding && finding.enabled && finding.problem) {
            notice("warn", "Faces cannot be found now: " + finding.problem);
        } else if (groups && groups.due && peopleOn()) {
            notice("info", "Faces changed (found, taken out of a person, or put back among the " +
                   "faces), or the settings did, since the faces were last grouped.",
                   [el("button", {type: "button", className: "button small",
                                  text: "Group faces again", on: {click: groupAgain}})]);
        } else if (groups && groups.faces && !peopleOn()) {
            notice("info", "People is off (config.json): faces are not looked for, nor grouped " +
                   "again. What was found, and what you said about it, is kept until you " +
                   "delete the face data (Manage).");
        } else {
            notice("", "");
        }
    }

    function card(person) {
        var named = Boolean(person.name);
        var node = el("article", {className: "card person-card" + (person.hidden ? " is-hidden" : "") +
                                  (named ? "" : " is-unnamed")});
        var target = adminOn ? {view: "person", id: person.id} : null;
        var cover = target ? link("", target, "person-cover")
            : el("a", {className: "person-cover", href: searchUrl(person)});
        cover.setAttribute("aria-label", (adminOn ? "Open " : "The pictures of ") + person.label);
        cover.appendChild(faceImage(person.cover, ""));
        node.appendChild(cover);
        var name = el("h2", {className: "person-name"}, [
            target ? link(person.label, target) : el("a", {href: searchUrl(person),
                                                           text: person.label})
        ]);
        if (person.hidden) {
            name.appendChild(el("span", {className: "person-badge", text: "Hidden"}));
        }
        node.appendChild(el("div", {}, [
            name,
            el("p", {className: "person-facts", text: picturesText(person)})
        ]));
        var actions = el("div", {className: "person-actions"});
        actions.appendChild(el("a", {className: "button small" + (adminOn ? "" : " primary"),
                                     href: searchUrl(person), text: "Pictures"}));
        if (adminOn) {
            actions.appendChild(el("button", {type: "button", className: "button small",
                                              text: named ? "Rename" : "Name",
                                              on: {click: function () {
                                                  nameDialog(person);
                                              }}}));
        }
        node.appendChild(actions);
        return node;
    }

    function listed() {
        var wantedText = sameText(findText);
        return people.filter(function (person) {
            if (person.hidden && !showHidden) {
                return false;
            }
            return !wantedText || sameText(person.label).indexOf(wantedText) >= 0;
        });
    }

    function drawCards(grid) {
        clear(grid);
        var list = listed();
        list.forEach(function (person) {
            grid.appendChild(card(person));
        });
        if (!list.length) {
            grid.appendChild(el("p", {className: "muted", text: findText
                ? "Nobody's name has \u201c" + findText + "\u201d in it."
                : "Nobody to show."}));
        }
    }

    // What the admin can do while nobody is listed.
    function emptyHint(groups) {
        if (groups && groups.faces) {
            return (peopleOn() ? "Group faces again above, or look at the faces in Other " +
                                 "faces: " : "Look at the faces in Other faces: ") +
                "a person needs their face in a few pictures taken at different moments.";
        }
        if (!peopleOn()) {
            return "Turn People on (see SETTINGS.md, \u201cpeople\u201d), then Find faces on " +
                "the Manage page.";
        }
        if (status.people.problem) {
            return "Put the setup right (see the notice above), then Find faces on the Manage " +
                "page.";
        }
        return "Find faces on the Manage page (each library's card), or scan: faces are " +
            "looked for after each scan.";
    }

    function renderList() {
        selection();
        byId("people-title").textContent = "People";
        document.title = "People - " + SITE.title;
        renderHead();
        dueNotice();
        var body = byId("people-body");
        clear(body);
        var line = statusText();
        if (line) {
            body.appendChild(el("p", {className: "people-status", text: line}));
        }
        if (adminOn) {
            body.appendChild(el("div", {id: "people-queue-entry"}));
            drawQueueEntry();
        }
        if (!people.length) {
            var groups = status && status.groups;
            body.appendChild(el("div", {className: "card"}, [
                el("p", {text: !adminOn ? "There is nobody to show."
                    : groups && groups.faces ? "No faces were grouped into people yet."
                    : "No faces were found yet."}),
                adminOn ? el("p", {className: "field-hint", text: emptyHint(groups)}) : null,
                adminOn && groups && groups.other
                    ? link("Other faces (" + fmt.number(groups.other) + ")",
                           {view: "other"}, "button small") : null
            ]));
            return;
        }
        var tools = el("div", {className: "people-tools"});
        var find = el("input", {type: "search", className: "input people-find",
                                placeholder: "Find a person", value: findText,
                                attrs: {"aria-label": "Find a person by name"}});
        tools.appendChild(find);
        if (adminOn) {
            var hiddenCount = people.filter(function (person) {
                return person.hidden;
            }).length;
            var toggle = el("input", {type: "checkbox", checked: showHidden});
            toggle.addEventListener("change", function () {
                showHidden = toggle.checked;
                SHELL.storageSet(HIDDEN_KEY, showHidden ? "1" : "0");
                drawCards(grid);
            });
            tools.appendChild(el("label", {className: "people-check"}, [
                toggle, "Show hidden people (" + fmt.number(hiddenCount) + ")"]));
            var groups = status && status.groups;
            tools.appendChild(link("Other faces" + (groups ? " (" + fmt.number(groups.other) + ")"
                                                   : ""), {view: "other"}, "button small"));
            if (groups && groups.not_faces) {
                tools.appendChild(link("Not faces (" + fmt.number(groups.not_faces) + ")",
                                       {view: "not_face"}, "button small"));
            }
        }
        body.appendChild(tools);
        var grid = el("div", {className: "people-grid"});
        body.appendChild(grid);
        find.addEventListener("input", function () {
            findText = find.value;
            drawCards(grid);
        });
        drawCards(grid);
    }

    /* ---------- Names, merges, the picker (admin) ----------------------------- */

    function nameDialog(person, then) {
        var box = SHELL.modal({title: person.name ? "Rename " + person.name : "Name " +
                               person.label});
        var input = el("input", {type: "text", className: "input", maxLength: 100,
                                 autocomplete: "off", spellcheck: false,
                                 value: person.name || ""});
        var field = SHELL.field("Name", input, {hint: person.name
            ? "Leave it empty to take the name away." : "As you want to find them: " +
              "person:\u201cName\u201d on the search page."});
        box.body.appendChild(field.node);
        var other = el("div", {hidden: true});
        box.body.appendChild(other);
        box.button("Cancel", "", function () {
            box.close();
        });
        var ok = box.button("Save", "primary", save);
        box.submitOnEnter(save);
        input.addEventListener("input", function () {
            field.error.hidden = true;
            other.hidden = true;
        });
        function save() {
            ok.disabled = true;
            SHELL.admin("/people/" + encodeURIComponent(person.id), {name: input.value},
                        {quiet: true}).then(function (reply) {
                box.close();
                SHELL.toast(reply.person.name ? "Named " + reply.person.name + "."
                            : "The name was taken away.", "ok");
                return reload().then(then);
            }, function (error) {
                ok.disabled = false;
                field.error.textContent = error && error.message ? error.message : "";
                field.error.hidden = !field.error.textContent;
                var otherId = error && error.data ? error.data.other : null;
                clear(other);
                if (typeof otherId === "number") {
                    // The name is another person's: perhaps they are one.
                    var namesake = personById(otherId);
                    other.appendChild(el("button", {type: "button", className: "button small",
                        text: "Merge into " + (namesake ? namesake.label : "that person"),
                        on: {click: function () {
                            box.close();
                            merge(person.id, otherId);
                        }}}));
                    other.hidden = false;
                }
            });
        }
        window.setTimeout(function () {
            input.focus();
            input.select();
        }, 0);
    }

    function merge(sourceId, targetId) {
        return SHELL.admin("/people/" + encodeURIComponent(sourceId) + "/merge",
                           {into: targetId}).then(function (reply) {
            SHELL.toast("Merged into " + reply.person.label + ".", "ok");
            return loadPeople().then(function () {
                return go({view: "person", id: reply.person.id}, true);
            });
        }, function () {
            return null;
        });
    }

    /* Choose a person (options.title, options.exclude: an id left out,
       options.allowNew: offer a new person). Resolves with {to: id} or {to:
       null, name} for a new person, or null when cancelled. */
    function pickPerson(options) {
        return new Promise(function (resolve) {
            var done = false;
            var box = SHELL.modal({title: options.title, onClose: function () {
                if (!done) {
                    done = true;
                    resolve(null);
                }
            }});
            var input = el("input", {type: "search", className: "input", autocomplete: "off",
                                     spellcheck: false, placeholder: options.allowNew
                                         ? "Find a person, or type a new name"
                                         : "Find a person"});
            var field = SHELL.field(options.allowNew ? "Person (or a new name)" : "Person", input);
            box.body.appendChild(field.node);
            var list = el("ul", {className: "person-pick-list"});
            box.body.appendChild(list);
            box.button("Cancel", "", function () {
                box.close();
            });
            function choose(answer) {
                done = true;
                resolve(answer);
                box.close();
            }
            var onEnter = null;         // what Enter chooses now (see the notes above)
            function option(image, text, small, answer) {
                // (The one Enter chooses is marked.)
                var chosen = answer === onEnter || (onEnter !== null && answer.to !== null &&
                                                    answer.to === onEnter.to);
                return el("li", {}, [el("button", {type: "button",
                                                   className: "person-pick" +
                                                       (chosen ? " is-enter" : ""),
                                                   title: chosen ? "Enter chooses this" : "",
                                                   on: {click: function () {
                                                       choose(answer);
                                                   }}}, [image, el("span", {text: text}),
                                                         small ? el("small", {text: small}) : null])]);
            }
            function draw() {
                clear(list);
                var text = input.value.replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
                var key = sameText(text);
                var matching = people.filter(function (person) {
                    return person.id !== options.exclude &&
                        (!key || sameText(person.label).indexOf(key) >= 0);
                });
                var exact = key ? matching.filter(function (person) {
                    return sameText(person.label) === key;
                }) : [];
                // (The one called so first, then the others as they are listed.)
                matching = exact.concat(matching.filter(function (person) {
                    return exact.indexOf(person) < 0;
                }));
                var fresh = options.allowNew && !exact.length ? {to: null, name: text} : null;
                onEnter = exact.length ? {to: exact[0].id}
                    : fresh && key ? fresh
                    : matching.length === 1 ? {to: matching[0].id} : null;
                matching.slice(0, 60).forEach(function (person) {
                    list.appendChild(option(faceImage(person.cover, ""), person.label,
                                            picturesText(person) + (person.hidden ? ", hidden" : ""),
                                            {to: person.id}));
                });
                if (fresh) {
                    list.appendChild(option(el("span", {className: "person-pick-new", text: "+",
                                                        attrs: {"aria-hidden": "true"}}),
                                            text ? "A new person called \u201c" + text + "\u201d"
                                                : "A new person (no name yet)", "", fresh));
                }
                if (!list.firstChild) {
                    list.appendChild(el("li", {className: "muted", text: "Nobody is called so."}));
                }
            }
            input.addEventListener("input", draw);
            box.submitOnEnter(function () {
                if (onEnter) {
                    choose(onEnter);
                }
            });
            draw();
            window.setTimeout(function () {
                input.focus();
            }, 0);
        });
    }

    function groupAgain() {
        SHELL.admin("/jobs", {kind: "group_faces", library_id: null}).then(function () {
            forgetUndo();       // (a grouping takes them away: see admin.py)
            drawQueueEntry();
            SHELL.toast("The faces will be grouped again; this page shows the people when it " +
                        "has finished.", "info");
            var before = status && status.groups ? status.groups.grouped_at : null;
            var tries = 0;
            window.clearTimeout(pollTimer);
            function poll() {
                tries += 1;
                loadStatus().then(function (answer) {
                    var at = answer && answer.groups ? answer.groups.grouped_at : null;
                    if (at && at !== before) {
                        reload();
                    } else if (tries < GROUP_TRIES) {
                        pollTimer = window.setTimeout(poll, GROUP_POLL);
                    }
                });
            }
            pollTimer = window.setTimeout(poll, GROUP_POLL);
        }, function () {
            return null;
        });
    }

    /* ---------- Faces: tiles, selection and what to do with them -------------- */

    function selectedIds() {
        return Object.keys(selected).map(Number).filter(function (id) {
            return selected[id];
        });
    }

    function faceTile(face, label) {
        var item = el("li", {className: "face-item"});
        var video = typeof face.at === "number" && isFinite(face.at);
        var tile = el("button", {type: "button", className: "face-tile",
                                 attrs: {"aria-pressed": selected[face.id] ? "true" : "false",
                                         "aria-label": "Face " + face.id +
                                             (label ? " of " + label : "") +
                                             (video ? ", in a video at " + momentText(face.at)
                                                 : "") +
                                             (face.owner ? ", placed by you" : "")},
                                 title: face.owner ? "You put this face here" : ""});
        tile.appendChild(faceImage(face.id, ""));
        tile.addEventListener("click", function () {
            selected[face.id] = !selected[face.id];
            tile.setAttribute("aria-pressed", selected[face.id] ? "true" : "false");
            selection();
        });
        item.appendChild(tile);
        if (face.owner) {
            item.appendChild(el("span", {className: "face-owner", attrs: {"aria-hidden": "true"}}));
        }
        if (face.image !== null && face.image !== undefined) {
            // (A video's: its moment, which it opens at.)
            item.appendChild(el("a", {className: "face-open" + (video ? " is-video" : ""),
                                      href: faceUrl(face),
                                      text: video ? "\u25b6 " + momentText(face.at) : "Picture",
                                      title: video ? "Open the video at " + momentText(face.at) +
                                          ", where this face is"
                                          : "Open the picture this face is in",
                                      attrs: {"aria-label": (video ? "Open the video of face "
                                                             : "Open the picture of face ") +
                                                            face.id}}));
        }
        return item;
    }

    function faceGrid(list, label) {
        var grid = el("ul", {className: "face-grid"});
        list.forEach(function (face) {
            grid.appendChild(faceTile(face, label));
        });
        return grid;
    }

    function setAll(on) {
        selected = {};
        if (on) {
            faces.forEach(function (face) {
                selected[face.id] = true;
            });
        }
        Array.prototype.forEach.call(document.querySelectorAll(".face-tile"), function (tile) {
            tile.setAttribute("aria-pressed", on ? "true" : "false");
        });
        selection();
    }

    function barButton(text, onClick, className, title) {
        return el("button", {type: "button", className: "button small" +
                             (className ? " " + className : ""), text: text, title: title || "",
                             on: {click: onClick}});
    }

    // The bar of what can be done with the selected faces.
    function selection() {
        var bar = byId("people-selection");
        clear(bar);
        var ids = selectedIds();
        if (!adminOn || where.view === "list" || where.view === "queue" || !faces.length) {
            bar.hidden = true;
            return;
        }
        bar.hidden = false;
        bar.appendChild(el("span", {className: "people-selection-count",
                                    text: ids.length ? fmt.plural(ids.length, "face") +
                                        " selected" : "Select faces"}));
        if (ids.length < faces.length) {
            bar.appendChild(barButton("Select all", function () {
                setAll(true);
            }));
        }
        if (ids.length) {
            bar.appendChild(barButton("Clear", function () {
                setAll(false);
            }));
        }
        if (!ids.length) {
            return;
        }
        if (where.view === "person" && shown) {
            var person = shown.person;
            bar.appendChild(barButton("Not " + person.label, function () {
                act({action: "reject", faces: ids, person: person.id},
                    fmt.plural(ids.length, "face") + " taken out of " + person.label + ".");
            }, "", "These faces are not " + person.label + ": they leave for good"));
            bar.appendChild(barButton("Move to\u2026", function () {
                pickPerson({title: "Move " + fmt.plural(ids.length, "face") + " to",
                            exclude: person.id, allowNew: true}).then(function (choice) {
                    if (choice) {
                        act({action: "move", faces: ids, to: choice.to, name: choice.name || "",
                             person: person.id}, fmt.plural(ids.length, "face") + " moved.");
                    }
                });
            }));
            if (ids.length === 1) {
                bar.appendChild(barButton("Use as cover", function () {
                    SHELL.admin("/people/" + encodeURIComponent(person.id), {cover: ids[0]})
                        .then(function () {
                            SHELL.toast("The cover of " + person.label + " was changed.", "ok");
                            reload();
                        }, function () {
                            return null;
                        });
                }));
            }
            bar.appendChild(barButton("Not a face", function () {
                act({action: "not_face", faces: ids, person: person.id},
                    fmt.plural(ids.length, "face") + " marked as not a face.");
            }, "danger-text subtle-danger"));
        } else if (where.view === "other") {
            bar.appendChild(barButton("Add to a person\u2026", function () {
                pickPerson({title: "Add " + fmt.plural(ids.length, "face") + " to",
                            allowNew: true}).then(function (choice) {
                    if (choice) {
                        act({action: "move", faces: ids, to: choice.to, name: choice.name || ""},
                            fmt.plural(ids.length, "face") + " put in a person.");
                    }
                });
            }, "primary"));
            bar.appendChild(barButton("Not a face", function () {
                act({action: "not_face", faces: ids}, fmt.plural(ids.length, "face") +
                    " marked as not a face.");
            }, "danger-text subtle-danger"));
        } else if (where.view === "not_face") {
            bar.appendChild(barButton("It is a face", function () {
                act({action: "is_face", faces: ids}, fmt.plural(ids.length, "face") +
                    " back among the faces.");
            }, "primary"));
        }
    }

    function act(request, done) {
        var buttons = byId("people-selection").querySelectorAll("button");
        Array.prototype.forEach.call(buttons, function (button) {
            button.disabled = true;
        });
        return SHELL.admin("/people/faces", request).then(function (reply) {
            SHELL.toast(done, "ok");
            selected = {};
            if (where.view === "person" && reply && (reply.source === null ||
                                                     reply.person === null)) {
                // (Its last faces went: the person is gone. The list takes the place of
                // its address, so Back does not lead to it again.)
                window.history.replaceState(null, "", addressOf({view: "list"}));
                return Promise.all([loadPeople(), loadStatus()]).then(function () {
                    return go({view: "list"}, false);
                });
            }
            return reload();
        }, function () {
            selection();
        });
    }

    /* ---------- One person (admin) -------------------------------------------- */

    function similarCard(person, other) {
        var node = el("article", {className: "card person-card" +
                                  (other.hidden ? " is-hidden" : "")});
        var target = {view: "person", id: other.id};
        var cover = link("", target, "person-cover");
        cover.setAttribute("aria-label", "Open " + other.label);
        cover.appendChild(faceImage(other.cover, ""));
        node.appendChild(cover);
        node.appendChild(el("div", {}, [
            el("h2", {className: "person-name"}, [link(other.label, target)]),
            el("p", {className: "person-facts", text: fmt.plural(other.faces, "face")})
        ]));
        node.appendChild(el("div", {className: "person-actions"}, [
            el("button", {type: "button", className: "button small primary", text: "Merge here",
                          title: "The same person: every face of " + other.label + " goes to " +
                              person.label,
                          on: {click: function () {
                              merge(other.id, person.id);
                          }}})
        ]));
        return node;
    }

    function renderPerson() {
        var person = shown.person;
        var listedPerson = personById(person.id);
        byId("people-title").textContent = person.label;
        document.title = person.label + " - People - " + SITE.title;
        byId("people-lead").textContent = "Select faces that are not " + person.label +
            ", or move them to the right person.";
        var actions = byId("people-actions");
        clear(actions);
        actions.appendChild(link("All people", {view: "list"}, "button"));
        notice("", "");
        var body = byId("people-body");
        clear(body);
        var top = el("div", {className: "person-top"});
        var cover = el("span", {className: "person-cover"});
        cover.appendChild(faceImage(listedPerson ? listedPerson.cover : person.cover, ""));
        top.appendChild(cover);
        var text = el("div", {className: "person-top-text"});
        if (person.hidden) {
            byId("people-title").appendChild(el("span", {className: "person-badge",
                                                         text: "Hidden"}));
        }
        text.appendChild(el("p", {text: fmt.plural(person.faces, "face") +
                                  (listedPerson ? " in " + picturesText(listedPerson) : "") +
                                  ". A green dot marks a face you put here."}));
        var tools = el("div", {className: "person-actions"});
        if (listedPerson) {
            tools.appendChild(el("a", {className: "button small primary",
                                       href: searchUrl(listedPerson), text: "Pictures"}));
        }
        tools.appendChild(barButton(person.name ? "Rename" : "Name", function () {
            nameDialog(person);
        }));
        tools.appendChild(barButton("Merge into\u2026", function () {
            pickPerson({title: "Merge " + person.label + " into", exclude: person.id})
                .then(function (choice) {
                    if (choice && choice.to !== null) {
                        merge(person.id, choice.to);
                    }
                });
        }, "", "The same person as another one: every face goes there"));
        tools.appendChild(barButton(person.hidden ? "Show again" : "Hide", function () {
            SHELL.admin("/people/" + encodeURIComponent(person.id), {hidden: !person.hidden})
                .then(function (reply) {
                    SHELL.toast(reply.person.hidden ? person.label + " is hidden: not listed, " +
                                "nor offered in searches." : person.label + " is shown again.",
                                "ok");
                    reload();
                }, function () {
                    return null;
                });
        }, "", person.hidden ? "" : "Not listed and not offered in searches (strangers)"));
        tools.appendChild(barButton("None are faces", function () {
            SHELL.confirm({title: "None of these are faces?",
                           message: "Every face of " + person.label + " is marked as not a " +
                               "face (things, such as wheels, toys or statues), and " +
                               person.label + " goes" + (person.name ? ", with its name" : "") +
                               ". Not faces on the People page takes the mark back; the " +
                               "faces then wait in Other faces for Group faces again.",
                           confirmText: "Mark them", danger: true}).then(function (yes) {
                if (yes) {
                    act({action: "not_face", faces: "all", person: person.id},
                        "Marked as not faces.");
                }
            });
        }, "danger-text subtle-danger"));
        text.appendChild(tools);
        top.appendChild(text);
        body.appendChild(top);
        if (shown.similar && shown.similar.length) {
            var section = el("section", {className: "people-section"}, [
                el("h3", {text: "Maybe the same person"}),
                el("p", {className: "field-hint", text: "Their faces look much like " +
                    person.label + "'s. Merge here those who are."})
            ]);
            var grid = el("div", {className: "similar-grid"});
            shown.similar.forEach(function (other) {
                grid.appendChild(similarCard(person, other));
            });
            section.appendChild(grid);
            body.appendChild(section);
        }
        var facesSection = el("section", {className: "people-section"}, [
            el("h3", {text: "Faces"}),
            el("p", {className: "field-hint", text: "Best first. Click faces to select them."})
        ]);
        facesSection.appendChild(faceGrid(faces, person.label));
        body.appendChild(facesSection);
        selection();
    }

    function showPerson(id, fresh) {
        if (fresh) {
            clear(byId("people-body"));
            byId("people-body").appendChild(el("p", {className: "message", text: "Loading..."}));
        }
        return SHELL.admin("/people/" + encodeURIComponent(id), undefined, {quiet: true})
            .then(function (answer) {
                shown = answer;
                faces = answer.faces || [];
                Object.keys(selected).forEach(function (key) {
                    if (!faces.some(function (face) {
                        return String(face.id) === key;
                    })) {
                        delete selected[key];
                    }
                });
                renderPerson();
            }, function (error) {
                shown = null;
                faces = [];
                renderList();
                notice("warn", error && error.status === 404
                    ? "That person is not there any more (merged, or its faces left)."
                    : (error && error.message) || "The person could not be loaded.");
            });
    }

    /* ---------- Other faces, and faces marked not a face (admin) -------------- */

    function renderOther(which, answer, more) {
        var other = which === "other";
        byId("people-title").textContent = other ? "Other faces" : "Not faces";
        document.title = byId("people-title").textContent + " - People - " + SITE.title;
        byId("people-lead").textContent = other
            ? "Faces in no person, best first: too few pictures of them, too small or turned " +
              "away, or taken out of a person. Put them in a person, or mark what is no face."
            : "What you marked as not a face. Select the ones that are, and take the mark back.";
        var actions = byId("people-actions");
        clear(actions);
        actions.appendChild(link("All people", {view: "list"}, "button"));
        notice("", "");
        var body = byId("people-body");
        if (!more) {
            clear(body);
            var tabs = el("div", {className: "people-tabs"});
            [["other", "Other faces"], ["not_face", "Not faces"]].forEach(function (tab) {
                var node = link(tab[1], {view: tab[0]}, "button small");
                if (tab[0] === which) {
                    node.setAttribute("aria-current", "page");
                }
                tabs.appendChild(node);
            });
            body.appendChild(tabs);
            body.appendChild(el("p", {className: "people-status",
                                      text: fmt.plural(answer.total, "face")}));
            body.appendChild(el("ul", {className: "face-grid", id: "people-faces"}));
        }
        var grid = byId("people-faces");
        (answer.faces || []).forEach(function (face) {
            grid.appendChild(faceTile(face, ""));
        });
        var pager = byId("people-more");
        if (pager) {
            pager.parentNode.removeChild(pager);
        }
        if (faces.length < answer.total) {
            body.appendChild(el("p", {className: "people-more", id: "people-more"}, [
                barButton("Show more", function () {
                    otherOffset = faces.length;
                    showOther(which, false, true);
                })
            ]));
        }
        selection();
    }

    function showOther(which, fresh, more) {
        if (fresh) {
            clear(byId("people-body"));
            byId("people-body").appendChild(el("p", {className: "message", text: "Loading..."}));
        }
        var offset = more ? otherOffset : 0;
        return SHELL.admin("/people/other?which=" + which + "&offset=" + offset, undefined,
                           {quiet: true}).then(function (answer) {
            faces = more ? faces.concat(answer.faces || []) : (answer.faces || []);
            if (!more) {
                selected = {};
            }
            renderOther(which, answer, more);
        }, function (error) {
            notice("error", (error && error.message) || "The faces could not be loaded.");
        });
    }

    /* ---------- "Who's this?": one face or unnamed person at a time (admin) ---- */

    /* The queue (GET /api/admin/people/queue, people_queue.py): every face in
       no person and every unnamed person whose suggestion is marked, worked
       out once and kept (the browser asks again by its ETag). It is walked
       one item at a time; each is asked about again right before it shows
       (GET /people/suggest, by its ETag) and passed over when it is no
       longer marked: an earlier answer, a grouping or another tab changed
       it. The answers go through the People page's own routes with "undo":
       true and "from" (where the faces were: 409 when a grouping moved them
       meanwhile, and the item is passed over with a note). */

    // (Of this face data: a face's id is another face's after Delete face data.)
    function skipPrefix() {
        return queue && typeof queue.data === "string" && queue.data ? queue.data + "/" : "";
    }

    function skipKey(item) {
        return skipPrefix() + (item.kind === "person" ? "p" + item.person : "f" + item.face) +
            ":" + item.to;
    }

    function readSkips() {
        var list = [];
        try {
            list = JSON.parse(SHELL.storageGet(SKIPS_KEY) || "[]");
        } catch (error) {
            list = [];
        }
        var found = {};
        (Array.isArray(list) ? list : []).forEach(function (key) {
            if (typeof key === "string") {
                found[key] = true;
            }
        });
        return found;
    }

    function writeSkips(found) {
        var prefix = skipPrefix();
        var list = Object.keys(found).filter(function (key) {
            return found[key] && key.indexOf(prefix) === 0;
        });
        SHELL.storageSet(SKIPS_KEY, list.length ? JSON.stringify(list.slice(-SKIPS_MAX)) : "");
    }

    function rememberSkip(item, on) {
        var found = readSkips();
        if (on) {
            found[skipKey(item)] = true;
        } else {
            delete found[skipKey(item)];
        }
        writeSkips(found);
    }

    // (This tab's storage; none -- turned off, or full -- keeps nothing.)
    function sessionGet(key) {
        try {
            return window.sessionStorage ? window.sessionStorage.getItem(key) : null;
        } catch (error) {
            return null;
        }
    }

    function sessionSet(key, value) {
        try {
            if (value) {
                window.sessionStorage.setItem(key, value);
            } else {
                window.sessionStorage.removeItem(key);
            }
        } catch (error) {
            // (Undo then lasts as long as the page.)
        }
    }

    function queueData(answer) {
        return answer && typeof answer.data === "string" ? answer.data : "";
    }

    // An item as it came in the queue (without what the walk noted on it).
    function plainItem(item) {
        var copy = {};
        Object.keys(item || {}).forEach(function (key) {
            if (key !== "result") {
                copy[key] = item[key];
            }
        });
        return copy;
    }

    /* The walk's answers Undo can take back, kept for this tab (UNDO_KEY) as
       {data: the queue's face data, steps: [{choice, to, token, item,
       person}]}, the last one last: those after the last answer that cannot
       be undone (the server let go of the ones before it), skips left out
       (this browser keeps those: SKIPS_KEY, and the list offers them again). */
    function keepUndo(history) {
        var steps = [];
        history.forEach(function (entry) {
            if (entry.choice === "skip") {
                return;
            }
            if (!entry.token) {
                steps = [];
                return;
            }
            steps.push({choice: entry.choice, to: entry.to, token: entry.token,
                        item: plainItem(entry.item), person: entry.person || null});
        });
        steps = steps.slice(-UNDO_KEEP);
        sessionSet(UNDO_KEY, steps.length ? JSON.stringify({data: queueData(queue), steps: steps})
            : "");
    }

    function forgetUndo() {
        sessionSet(UNDO_KEY, "");
    }

    // The answers kept for Undo (keepUndo) of answer's face data, as a walk's
    // history (at null: not one of the walk's items); of other face data
    // (Delete face data numbers the faces anew) they are let go.
    function keptUndo(answer) {
        var kept = null;
        try {
            kept = JSON.parse(sessionGet(UNDO_KEY) || "null");
        } catch (error) {
            kept = null;
        }
        if (!kept || typeof kept !== "object" || !Array.isArray(kept.steps) ||
                kept.data !== queueData(answer)) {
            if (kept) {
                forgetUndo();
            }
            return [];
        }
        return kept.steps.filter(function (entry) {
            return entry && typeof entry.token === "string" && entry.token &&
                ["yes", "not", "not_face"].indexOf(entry.choice) >= 0 &&
                entry.item && (entry.item.kind === "face" || entry.item.kind === "person");
        }).map(function (entry) {
            return {at: null, choice: entry.choice, to: entry.to, token: entry.token,
                    item: entry.item, person: entry.person || null};
        });
    }

    function queueItems(answer) {
        return answer && Array.isArray(answer.items) ? answer.items : [];
    }

    // The queue's items not skipped before (with skipped: those skipped).
    function toConfirm(answer, skipped) {
        var skips = readSkips();
        return queueItems(answer).filter(function (item) {
            return Boolean(skips[skipKey(item)]) === Boolean(skipped);
        });
    }

    function wait(ms) {
        return new Promise(function (resolve) {
            window.setTimeout(resolve, ms);
        });
    }

    // The queue, asked for again while the server works it out (202); one
    // request at a time. Resolves with it, or null when it cannot be had.
    function loadQueue() {
        if (!adminOn) {
            return Promise.resolve(null);
        }
        if (queueLoading) {
            return queueLoading;
        }
        var tries = 0;
        // (Asked again with the number of the build the server works on: its
        // answer, or a later one's, even while the faces change meanwhile.)
        function ask(build) {
            return SHELL.admin("/people/queue" + (build ? "?build=" + encodeURIComponent(build)
                                                  : ""), undefined, {quiet: true})
                .then(function (answer) {
                    if (answer && answer.building) {
                        tries += 1;
                        return tries > QUEUE_TRIES ? null : wait(QUEUE_POLL).then(function () {
                            return ask(answer.build || build);
                        });
                    }
                    queue = answer || null;
                    return queue;
                });
        }
        queueLoading = ask(0).then(function (answer) {
            queueLoading = null;
            return answer;
        }, function (error) {
            queueLoading = null;
            throw error;
        });
        return queueLoading;
    }

    // The list's "Who's this? 12 to confirm" (when there are any); the
    // keyboard stays on it when it was there (the buttons are made anew).
    function drawQueueEntry() {
        var box = byId("people-queue-entry");
        if (!box) {
            return;
        }
        var focused = document.activeElement;
        while (focused && focused !== box) {
            focused = focused.parentNode;
        }
        drawEntryBox(box);
        if (focused) {
            focusEntry();
        }
    }

    function drawEntryBox(box) {
        clear(box);
        var count = toConfirm(queue).length;
        var skipped = toConfirm(queue, true).length;
        // (Answers given in Who's this? before, still to be undone.)
        var undoable = queue ? keptUndo(queue) : [];
        var last = undoable[undoable.length - 1];
        if (!count && !skipped && !last) {
            return;
        }
        var entry = el("section", {className: "card queue-entry",
                                   attrs: {"aria-labelledby": "queue-entry-title"}});
        entry.appendChild(el("div", {className: "queue-entry-text"}, [
            el("h2", {id: "queue-entry-title", text: "Who\u2019s this?"}),
            el("p", {text: count
                ? fmt.number(count) + " to confirm: faces and unnamed people the app is " +
                  "sure about, one at a time."
                : "Nothing more to confirm now."})
        ]));
        var actions = el("div", {className: "person-actions"});
        if (count) {
            actions.appendChild(link("Start", {view: "queue"}, "button primary"));
        }
        if (skipped) {
            actions.appendChild(barButton("Show the " + fmt.plural(skipped, "skipped one") +
                                          " again", function () {
                writeSkips({});
                drawQueueEntry();
            }, "", "Ask about the ones you skipped again"));
        }
        if (last) {
            actions.appendChild(barButton("Undo last answer", function () {
                undoKept(last);
            }, "", "Undo your last answer in Who\u2019s this? (" + answerText(last) + ")"));
        }
        entry.appendChild(actions);
        box.appendChild(entry);
    }

    // The list's "Undo last answer": the last answer kept (keepUndo) undone, then
    // the people and the queue as they are now (it is asked about again there).
    function undoKept(entry) {
        var buttons = byId("people-queue-entry").querySelectorAll("button");
        Array.prototype.forEach.call(buttons, function (button) {
            button.disabled = true;
        });
        SHELL.admin("/people/undo", {undo: entry.token}, {quiet: true}).then(function () {
            var steps = keptUndo(queue);
            steps.pop();
            keepUndo(steps);
            SHELL.toast("Undone: " + answerText(entry) + ".", "ok");
            // (The queue asked for anew, after one on its way: the answer undone is
            // to be confirmed again.)
            var before = queueLoading ? queueLoading.catch(function () {
                return null;
            }) : null;
            return Promise.resolve(before).then(function () {
                return Promise.all([loadPeople(), loadStatus(), loadQueue().catch(function () {
                    return null;
                })]);
            }).then(function () {
                if (where.view === "list") {
                    show(where, false, true);
                    focusEntry();
                }
            });
        }, function (error) {
            if (error && error.status === 409 && !(error.data && error.data.kept)) {
                forgetUndo();   // (the server let go of them)
            }
            SHELL.toast((error && error.message) || "That cannot be undone.", "error");
            if (where.view === "list") {
                drawQueueEntry();
                focusEntry();
            }
        });
    }

    function refreshQueueEntry() {
        loadQueue().then(drawQueueEntry, function () {
            return null;
        });
    }

    // The keyboard on the list's "Who's this?" (its Start, or its button), else
    // on the page's title.
    function focusEntry() {
        var box = byId("people-queue-entry");
        var target = box ? box.querySelector("a") || box.querySelector("button") : null;
        if (!target) {
            target = byId("people-title");
            target.setAttribute("tabindex", "-1");
        }
        target.focus();
    }

    // The queue kept without the items a walk decided or passed over (until it
    // is asked for again).
    function dropDecided(left) {
        if (!queue) {
            return;
        }
        var done = {};
        left.items.forEach(function (item) {
            if (item.result && item.result !== "skipped") {
                done[skipKey(item)] = true;
            }
        });
        var copy = {};
        Object.keys(queue).forEach(function (key) {
            copy[key] = queue[key];
        });
        copy.items = queueItems(queue).filter(function (item) {
            return !done[skipKey(item)];
        });
        queue = copy;
    }

    function suggested(item) {
        var person = queue && queue.people ? queue.people[String(item.to)] : null;
        return person || {id: item.to, label: "Person " + item.to, faces: 0, show: []};
    }

    // A button of the queue's: its text, its key (shown where there is a
    // keyboard) and the keys told to screen readers.
    function queueButton(text, key, aria, onClick, className) {
        return el("button", {type: "button", className: "button queue-button" +
                             (className ? " " + className : ""),
                             attrs: {"aria-keyshortcuts": aria},
                             on: {click: onClick}},
                  [el("span", {text: text}),
                   el("kbd", {className: "queue-key", text: key,
                              attrs: {"aria-hidden": "true"}})]);
    }

    // (A picture that cannot be shown leaves no broken image behind.)
    function hideBroken(event) {
        event.target.hidden = true;
    }

    // (Pictures only: the heading above says whose; screen readers pass them by.)
    function faceStrip(ids) {
        var strip = el("ul", {className: "queue-strip", attrs: {"aria-hidden": "true"}});
        (ids || []).forEach(function (id) {
            strip.appendChild(el("li", {}, [faceImage(id, "")]));
        });
        return strip;
    }

    // The next item's faces, fetched while this one is looked at.
    function preload(item) {
        if (!item || typeof window.Image !== "function") {
            return;
        }
        var ids = [item.face].concat(item.show || [], suggested(item).show || []);
        ids.forEach(function (id) {
            var url = SITE.faceUrl(id);
            if (url) {
                var image = new window.Image();
                image.src = url;
            }
        });
    }

    function tallyText(items) {
        var tally = {named: 0, merged: 0, mergedFaces: 0, not: 0, not_face: 0, skipped: 0,
                     gone: 0};
        items.forEach(function (item) {
            if (item.result === "merged") {
                tally.mergedFaces += item.faces || 0;
            }
            if (item.result && tally[item.result] !== undefined) {
                tally[item.result] += 1;
            }
        });
        var parts = [];
        if (tally.named) {
            parts.push("named " + fmt.plural(tally.named, "face"));
        }
        if (tally.merged) {
            parts.push("named " + fmt.plural(tally.merged, "person", "people") + " (" +
                       fmt.plural(tally.mergedFaces, "face") + ")");
        }
        if (tally.not) {
            parts.push(fmt.number(tally.not) + " not who was suggested");
        }
        if (tally.not_face) {
            parts.push(fmt.plural(tally.not_face, "not a face", "not faces"));
        }
        if (tally.skipped) {
            parts.push(fmt.number(tally.skipped) + " skipped");
        }
        if (tally.gone) {
            parts.push(fmt.number(tally.gone) + " passed over (no longer sure, or changed)");
        }
        if (!parts.length) {
            return "Nothing was decided.";
        }
        var text = parts.join(", ") + ".";
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    function queueHead() {
        byId("people-title").textContent = "Who\u2019s this?";
        document.title = "Who\u2019s this? - People - " + SITE.title;
        byId("people-lead").textContent = "The faces and unnamed people the app is sure " +
            "about, one at a time. Nothing is decided without your key or click.";
        var actions = byId("people-actions");
        clear(actions);
        actions.appendChild(link("All people", {view: "list"}, "button"));
        notice("", "");
    }

    // A message instead of the item; focus: it takes the keyboard (the button
    // pressed went with the item).
    function queueMessage(text, focus) {
        var body = byId("people-body");
        clear(body);
        var message = el("p", {className: "message", attrs: {role: "status", tabindex: "-1"},
                               text: text});
        body.appendChild(message);
        if (focus) {
            message.focus();
        }
    }

    // The line above the item ("3 of 12 - a note"): one element of the page,
    // there while Who's this? shows (null: not).
    function progressLine(text) {
        var line = byId("queue-progress");
        if (line) {
            line.textContent = text || "";
            line.hidden = text === null;
        }
    }

    // "3 of 12", and what happened since the last item was shown.
    function progressText() {
        var notes = [];
        if (walk.note) {
            notes.push(walk.note);
        }
        if (walk.passed) {
            notes.push(fmt.number(walk.passed) + " passed over (changed meanwhile)");
        }
        return fmt.number(walk.at + 1) + " of " + fmt.number(walk.items.length) +
            (notes.length ? " \u2013 " + notes.join("; ") : "");
    }

    // A note on the line about the item shown (an undo that was not done), and
    // told to screen readers: the line is not a live region (it is read with
    // each item's question, and would be read twice), and the keyboard stays
    // where it is.
    function progressNote(text) {
        if (walk && walk.current) {
            progressLine(fmt.number(walk.at + 1) + " of " + fmt.number(walk.items.length) +
                         " \u2013 " + text);
            say(text);
        } else {
            SHELL.toast(text.charAt(0).toUpperCase() + text.slice(1) + ".", "info");
        }
    }

    // Tells screen readers a note (#queue-say, a polite live region of the
    // page; "": nothing to tell).
    function say(text) {
        var region = byId("queue-say");
        if (region) {
            // (The same note again is told again: one way or the other it changes.)
            region.textContent = text && region.textContent === text ? text + "\u00a0" : text;
        }
    }

    function showQueue() {
        queueHead();
        selection();
        progressLine(null);
        queueMessage("Looking for faces to confirm\u2026");
        return loadQueue().then(function (answer) {
            if (where.view !== "queue") {
                return null;
            }
            if (!answer) {
                queueMessage("The faces to confirm are still being worked out: come back " +
                             "in a moment.");
                return null;
            }
            startWalk(answer);
            return null;
        }, function (error) {
            if (where.view === "queue") {
                queueMessage("");
                notice("error", (error && error.message) ||
                       "The faces to confirm could not be loaded.",
                       [barButton("Try again", function () {
                           showQueue();
                       })]);
            }
        });
    }

    // (history: the answers given, the last one last -- {at: the item's place in
    // items, or null for one given before this walk, choice, to, token: the
    // server's undo, item, person: the one suggested} -- with those of earlier
    // walks this tab kept: keptUndo.)
    function startWalk(answer) {
        walk = {items: toConfirm(answer).map(plainItem), at: -1, current: null,
                history: keptUndo(answer), busy: false, note: "", passed: 0, shownAt: 0};
        next();
    }

    // Whether an item is still marked as it was (resolves true / false; a
    // request that fails rejects).
    function stillMarked(item) {
        return SHELL.admin("/people/suggest?face=" + encodeURIComponent(item.face), undefined,
                           {quiet: true}).then(function (answer) {
            if (!answer || answer.preselect !== item.to) {
                return false;
            }
            if (item.kind === "person") {
                // (Hidden meanwhile: a stranger; other faces: not what was listed.)
                return answer.basis === "person" && Boolean(answer.person) &&
                    answer.person.id === item.person && !answer.person.hidden &&
                    answer.person.faces === item.faces;
            }
            return answer.basis === "face";
        });
    }

    // The next item still marked (or the end).
    function next() {
        var mine = walk;
        mine.at += 1;
        mine.current = null;
        if (mine.at >= mine.items.length) {
            renderQueueEnd();
            return;
        }
        var item = mine.items[mine.at];
        mine.busy = true;
        stillMarked(item).then(function (still) {
            mine.busy = false;
            if (where.view !== "queue" || walk !== mine) {
                return;
            }
            if (!still) {
                item.result = "gone";
                mine.passed += 1;
                next();
                return;
            }
            mine.current = item;
            renderQueueItem(item);
            preload(mine.items[mine.at + 1]);
        }, function (error) {
            mine.busy = false;
            if (where.view !== "queue" || walk !== mine) {
                return;
            }
            mine.at -= 1;
            notice("error", (error && error.message) || "The next face could not be asked " +
                   "about.", [barButton("Try again", function () {
                       notice("", "");
                       next();
                   })]);
        });
    }

    function renderQueueItem(item) {
        var person = suggested(item);
        var name = person.label;
        var body = byId("people-body");
        clear(body);
        notice("", "");
        // (Read with the question when it takes the keyboard: aria-describedby.)
        progressLine(progressText());
        say("");
        walk.note = "";
        walk.passed = 0;
        var card = el("article", {className: "card queue-card",
                                  attrs: {"aria-labelledby": "queue-question"}});
        var question = el("h2", {className: "queue-question", id: "queue-question",
                                 attrs: {tabindex: "-1",
                                         "aria-describedby": "queue-progress queue-words"},
                                 text: item.kind === "person"
                                     ? "Is " + item.label + " " + name + "?"
                                     : "Is this " + name + "?"});
        card.appendChild(question);
        var words = String(item.words || "");
        card.appendChild(el("p", {className: "queue-words", id: "queue-words", text:
            words.charAt(0).toUpperCase() + words.slice(1) + " " + name +
            (typeof item.score === "number" ? " (" + item.score.toFixed(2) + ")" : "") +
            (item.kind === "person" ? "; " + item.label + " has " +
             fmt.plural(item.faces, "face") : "") + "."}));
        var sides = el("div", {className: "queue-sides"});
        var mine = el("div", {className: "queue-side"});
        if (item.kind === "face") {
            mine.appendChild(el("h3", {text: "This face"}));
            mine.appendChild(el("div", {className: "queue-face"}, [
                faceImage(item.face, "The face asked about")]));
            if (item.image !== null && item.image !== undefined) {
                var video = typeof item.at === "number" && isFinite(item.at);
                mine.appendChild(el("a", {className: "queue-picture", target: "_blank",
                                          rel: "noopener",
                                          href: faceUrl({id: item.face, image: item.image,
                                                         at: item.at})}, [
                    el("img", {src: SITE.thumbUrl(item.image), alt: "", loading: "lazy",
                               on: {error: hideBroken}}),
                    el("span", {text: video ? "Open the video at " + momentText(item.at)
                                    : "Open the picture"})]));
            }
        } else {
            mine.appendChild(el("h3", {text: item.label + "\u2019s clearest faces"}));
            mine.appendChild(faceStrip(item.show));
            mine.appendChild(el("a", {className: "queue-more", target: "_blank",
                                      rel: "noopener", href: personUrl(item.person),
                                      text: "All " + fmt.plural(item.faces, "face") + " of " +
                                          item.label}));
        }
        sides.appendChild(mine);
        var theirs = el("div", {className: "queue-side"});
        theirs.appendChild(el("h3", {text: name + "\u2019s clearest faces"}));
        theirs.appendChild(faceStrip(person.show));
        sides.appendChild(theirs);
        card.appendChild(sides);
        var keys = el("div", {className: "queue-keys"});
        keys.appendChild(queueButton("Yes, " + name, "Enter", "Enter", function () {
            decide("yes");
        }, "primary"));
        keys.appendChild(queueButton("Not " + name, "N", "N", function () {
            decide("not");
        }));
        keys.appendChild(queueButton("Skip", "S", "S ArrowRight", function () {
            decide("skip");
        }));
        if (item.kind === "face") {
            keys.appendChild(queueButton("Not a face", "X", "X", function () {
                decide("not_face");
            }, "danger-text subtle-danger"));
        } else {
            // (Its place kept: on a phone each answer is where it was for a face.)
            keys.appendChild(el("span", {className: "queue-slot",
                                         attrs: {"aria-hidden": "true"}}));
        }
        var undo = queueButton("Undo", "U", "U Control+Z Meta+Z", undoLast);
        undo.disabled = !walk.history.length;
        keys.appendChild(undo);
        keys.appendChild(queueButton("Leave", "Esc", "Escape", leaveQueue));
        card.appendChild(keys);
        body.appendChild(card);
        walk.shownAt = Date.now();
        question.focus();
    }

    function renderQueueEnd() {
        walk.current = null;
        walk.shownAt = Date.now();
        walk.note = "";
        walk.passed = 0;
        progressLine(null);
        say("");
        var body = byId("people-body");
        clear(body);
        var card = el("article", {className: "card queue-card queue-end",
                                  attrs: {"aria-labelledby": "queue-question"}});
        var heading = el("h2", {className: "queue-question", id: "queue-question",
                                attrs: {tabindex: "-1", "aria-describedby": "queue-tally"},
                                text: walk.items.length ? "That\u2019s all for now"
                                    : "Nothing to confirm now"});
        card.appendChild(heading);
        // (The ones skipped, this time or before: still marked when the queue came.)
        var skipped = toConfirm(queue, true).length;
        card.appendChild(el("p", {className: "queue-tally", id: "queue-tally",
                                  text: walk.items.length ? tallyText(walk.items)
                                      : skipped === 1 ? "Only the one you skipped is left."
                                      : skipped ? "Only the " + fmt.number(skipped) +
                                        " you skipped are left."
                                      : "The app is not sure about any face of nobody or " +
                                        "unnamed person now."}));
        var keys = el("div", {className: "queue-keys"});
        if (peopleOn()) {
            keys.appendChild(el("button", {type: "button", className: "button primary",
                                           text: "Group faces again to look for more",
                                           title: "Your answers can make the app sure about " +
                                               "more faces",
                                           on: {click: function () {
                                               if (!tooSoon()) {
                                                   groupThenQueue();
                                               }
                                           }}}));
        }
        if (skipped) {
            keys.appendChild(el("button", {type: "button", className: "button",
                                           text: "Show the " + fmt.plural(skipped,
                                                                          "skipped one") +
                                               " again",
                                           on: {click: function () {
                                               if (!tooSoon()) {
                                                   writeSkips({});
                                                   showQueue();
                                               }
                                           }}}));
        }
        if (walk.history.length) {
            keys.appendChild(queueButton("Undo", "U", "U Control+Z Meta+Z", undoLast));
        }
        // (Leave: not in the first moment either.)
        keys.appendChild(el("a", {className: "button", href: addressOf({view: "list"}),
                                  text: "All people", on: {click: function (event) {
                                      if (event.button === 0 && !event.ctrlKey &&
                                              !event.metaKey && !event.shiftKey) {
                                          event.preventDefault();
                                          leaveQueue();
                                      }
                                  }}}));
        card.appendChild(keys);
        body.appendChild(card);
        heading.focus();
    }

    // Group the faces again (the group_faces job), then the queue anew.
    function groupThenQueue() {
        var mine = walk;
        progressLine(null);
        queueMessage("Grouping the faces again: the next faces to confirm show here when " +
                     "it has finished.", true);
        var before = null;
        loadStatus().then(function (answer) {
            before = answer && answer.groups ? answer.groups.grouped_at : null;
            return SHELL.admin("/jobs", {kind: "group_faces", library_id: null});
        }).then(function () {
            // (A grouping takes the answers' undo away, see admin.py: the next walk
            // starts without them.)
            forgetUndo();
            var tries = 0;
            window.clearTimeout(pollTimer);
            function poll() {
                tries += 1;
                loadStatus().then(function (answer) {
                    if (where.view !== "queue" || walk !== mine) {
                        return;
                    }
                    var at = answer && answer.groups ? answer.groups.grouped_at : null;
                    if (at && at !== before) {
                        loadPeople().then(showQueue);
                    } else if (tries < GROUP_TRIES) {
                        pollTimer = window.setTimeout(poll, GROUP_POLL);
                    } else {
                        queueMessage("The grouping has not finished yet: come back to " +
                                     "Who\u2019s this? later.", true);
                    }
                });
            }
            pollTimer = window.setTimeout(poll, GROUP_POLL);
        }, function () {
            if (where.view === "queue" && walk === mine) {
                renderQueueEnd();
            }
        });
    }

    // (A person's answer says what it was like when shown, "seen": refused, and
    // passed over, when a grouping or another tab changed it since.)
    function requestFor(item, choice) {
        if (item.kind === "person" && choice === "yes") {
            return {path: "/people/" + encodeURIComponent(item.person) + "/merge",
                    body: {into: item.to, face: item.face, seen: item.seen, undo: true}};
        }
        var request = item.kind === "person"
            ? {faces: "all", from: item.person, seen: item.seen}
            : {faces: [item.face], from: null};
        if (choice === "yes") {
            request.action = "move";
            request.to = item.to;
        } else if (choice === "not") {
            request.action = "reject";
            request.person = item.to;
        } else {
            request.action = "not_face";
        }
        request.undo = true;
        return {path: "/people/faces", body: request};
    }

    function setQueueButtons(disabled) {
        Array.prototype.forEach.call(document.querySelectorAll(".queue-keys button"),
                                     function (button) {
            button.disabled = disabled;
        });
    }

    // Not yet: the item showed only a moment ago (a press meant for the last one).
    function tooSoon() {
        return Boolean(walk) && Date.now() - walk.shownAt < DECIDE_AFTER;
    }

    // An answer given (history's entry, see startWalk), kept for Undo.
    function answered(mine, item, choice, token) {
        mine.history.push({at: mine.at, choice: choice, to: item.to, token: token, item: item,
                           person: suggested(item)});
        keepUndo(mine.history);
    }

    function decide(choice) {
        var mine = walk;
        var item = mine && mine.current;
        if (!item || mine.busy || tooSoon()) {
            return;
        }
        if (choice === "not_face" && item.kind !== "face") {
            return;
        }
        if (choice === "skip") {
            rememberSkip(item, true);
            item.result = "skipped";
            answered(mine, item, choice, null);
            next();
            return;
        }
        mine.busy = true;
        setQueueButtons(true);
        var request = requestFor(item, choice);
        SHELL.admin(request.path, request.body, {quiet: true}).then(function (reply) {
            mine.busy = false;
            answered(mine, item, choice, reply && typeof reply.undo === "string" ? reply.undo
                : null);
            item.result = choice === "yes" ? (item.kind === "person" ? "merged" : "named")
                : choice;
            if (walk === mine && where.view === "queue") {
                next();
            }
        }, function (error) {
            mine.busy = false;
            if (walk !== mine || where.view !== "queue") {
                return;
            }
            if (error && (error.status === 409 || error.status === 404)) {
                // (A grouping or another tab moved the face, or took the person away.)
                item.result = "gone";
                mine.note = "the last one changed since it was shown, so it was passed over";
                next();
                return;
            }
            setQueueButtons(false);
            notice("error", (error && error.message) || "That did not work.");
        });
    }

    function undoLast() {
        var mine = walk;
        if (!mine || mine.busy || tooSoon()) {
            return;
        }
        var last = mine.history[mine.history.length - 1];
        if (!last) {
            progressNote("nothing to undo");
            return;
        }
        if (last.choice === "skip") {
            mine.history.pop();
            keepUndo(mine.history);
            rememberSkip(mine.items[last.at], false);
            mine.note = "undone: " + answerText(last);
            back(last);
            return;
        }
        if (!last.token) {
            progressNote("that answer cannot be undone here (another change came in " +
                         "between, or it was too large): put it right on the People page");
            return;
        }
        mine.busy = true;
        setQueueButtons(true);
        SHELL.admin("/people/undo", {undo: last.token}, {quiet: true}).then(function () {
            mine.busy = false;
            mine.history.pop();
            keepUndo(mine.history);
            if (walk === mine && where.view === "queue") {
                mine.note = "undone: " + answerText(last);
                back(last);
            }
        }, function (error) {
            mine.busy = false;
            if (error && error.status === 409 && !(error.data && error.data.kept)) {
                // (Refused for good: the server let go of the earlier answers too. A
                // busy server, or a later change to undo first, keeps them.)
                mine.history.forEach(function (entry) {
                    entry.token = null;
                });
                keepUndo(mine.history);
            }
            setQueueButtons(false);
            SHELL.toast((error && error.message) || "That cannot be undone.", "error");
        });
    }

    // What an answer was, in a word or two ("Not Ann").
    function answerText(entry) {
        var name = entry.person && entry.person.label ? entry.person.label
            : suggested({to: entry.to}).label;
        return entry.choice === "yes" ? "Yes, " + name
            : entry.choice === "not" ? "Not " + name
            : entry.choice === "not_face" ? "Not a face" : "Skip";
    }

    // Back to the item of an answer undone, asked about again (those passed
    // over after it may be marked again too). One given before this walk (at
    // null) is asked about here, before the one shown (in its place, when that
    // is the same item).
    function back(entry) {
        if (entry.at === null) {
            var at = Math.max(0, Math.min(walk.at, walk.items.length));
            var key = skipKey(entry.item);
            walk.items = walk.items.filter(function (item, k) {
                return k < at || item.result || skipKey(item) !== key;
            });
            walk.items.splice(at, 0, plainItem(entry.item));
            entry.at = at;
            var people = queue && queue.people ? queue.people : {};
            if (entry.person && !people[String(entry.item.to)] && queue) {
                // (Whom it was asked about, for its question and its faces to compare.)
                var copy = {};
                Object.keys(people).forEach(function (id) {
                    copy[id] = people[id];
                });
                copy[String(entry.item.to)] = entry.person;
                queue.people = copy;
            }
        }
        for (var k = entry.at; k < walk.items.length; k++) {
            if (k === entry.at || walk.items[k].result === "gone") {
                delete walk.items[k].result;
            }
        }
        walk.at = entry.at - 1;
        next();
    }

    // (Not in the first moment an item shows either: a press meant for it.)
    function leaveQueue() {
        if (!tooSoon()) {
            go({view: "list"}, true);   // (show() lets the walk go)
        }
    }

    function onQueueKey(event) {
        if (where.view !== "queue" || !walk || event.defaultPrevented) {
            return;
        }
        if (document.querySelector("dialog[open]")) {
            return;
        }
        var target = event.target || {};
        var tag = String(target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable) {
            return;
        }
        var key = event.key;
        if (event.repeat) {
            return;         // (a key held down: one answer per press, nor leaves)
        }
        if (key === "Escape" && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault();
            leaveQueue();
            return;
        }
        if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey &&
                (key === "z" || key === "Z")) {
            event.preventDefault();
            undoLast();
            return;
        }
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        if (key === "u" || key === "U") {
            event.preventDefault();
            undoLast();
            return;
        }
        if (!walk.current) {
            return;
        }
        if (key === "Enter") {
            if (tag === "button" || tag === "a") {
                return;         // (Enter on a button or a link does what it says)
            }
            event.preventDefault();
            decide("yes");
        } else if (key === "n" || key === "N") {
            event.preventDefault();
            decide("not");
        } else if (key === "s" || key === "S" || key === "ArrowRight") {
            event.preventDefault();
            decide("skip");
        } else if ((key === "x" || key === "X") && walk.current.kind === "face") {
            event.preventDefault();
            decide("not_face");
        }
    }

    /* ---------- Start ----------------------------------------------------------- */

    function start() {
        window.addEventListener("popstate", function () {
            selected = {};
            show(wanted(), true);
        });
        document.addEventListener("keydown", onQueueKey);
        // (The list asks for "Who's this?" beside the people, and waits for it a
        // moment, QUEUE_HOLD at most: drawn together, its entry pushes nothing down.)
        var held = adminOn && wanted().view === "list" ? Promise.race([
            loadQueue().then(function () {
                return true;
            }, function () {
                return false;
            }), wait(QUEUE_HOLD)]) : null;
        Promise.all([loadPeople(), loadStatus(), held]).then(function (answers) {
            return show(wanted(), true, answers[2] === true);
        });
    }

    start();
})();
