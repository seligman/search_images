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
        if (adminOn && (view === "other" || view === "not_face")) {
            return {view: view};
        }
        return {view: "list"};
    }

    function addressOf(place) {
        if (place.view === "person") {
            return personUrl(place.id);
        }
        if (place.view === "other" || place.view === "not_face") {
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

    function show(place, fresh) {
        where = place;
        if (place.view === "person") {
            return showPerson(place.id, fresh);
        }
        if (place.view === "other" || place.view === "not_face") {
            return showOther(place.view, fresh);
        }
        renderList();
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
        if (!adminOn || where.view === "list" || !faces.length) {
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

    /* ---------- Start ----------------------------------------------------------- */

    function start() {
        window.addEventListener("popstate", function () {
            selected = {};
            show(wanted(), true);
        });
        Promise.all([loadPeople(), loadStatus()]).then(function () {
            return show(wanted(), true);
        });
    }

    start();
})();
